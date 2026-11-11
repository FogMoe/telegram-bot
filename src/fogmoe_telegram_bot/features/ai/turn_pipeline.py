"""AI 对话三个入口（用户消息、定时任务、空闲跟进）在输入落库之后共用的执行流水线。

输入阶段各自不同（用户消息需要算费、扣费、识别图片；定时任务与空闲跟进需要管理租约与回顾事件），
但进入主模型之后的四段生命周期是一致的：

    model -> (可选：投递前门闸/阶段推进) -> history_out -> delivery -> finalize

入口之间的合理差异通过 `EntryPolicy` 显式声明，避免在三处复制同一套抑制历史、整理回复、
推导工具记录、处理运行时错误和零余额边界的逻辑。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from fogmoe_telegram_bot.core.chat_records import ZeroBalancePolicy
from fogmoe_telegram_bot.core.telegram_history import suppress_telegram_history
from fogmoe_telegram_bot.features.ai.outbound import finish_reply
from fogmoe_telegram_bot.features.ai.router import runtime_error_cause
from fogmoe_telegram_bot.features.ai.tool_history import (
    reply_record_entries,
    tool_logs_completed_clear,
    turn_record_entries,
)
from fogmoe_telegram_bot.features.ai.types import (
    LoopResult,
    LoopStatus,
    ToolLog,
    VisibleContentSink,
    has_side_effects,
)


class NoSideEffectsRetryError(RuntimeError):
    """主模型在产生任何外部副作用之前失败，调用方可安全延迟重试。"""


class RuntimeErrorPolicy(StrEnum):
    """主模型返回运行时错误通知（超时、上下文超限、部分失败、服务不可用）时的入口策略。"""

    # 用户消息与定时任务：向用户发送固定错误提示（走 bot_runtime error_notice 作用域，不写入 assistant 历史）。
    DELIVER_NOTICE = "deliver_notice"
    # 空闲跟进：无副作用时抛 NoSideEffectsRetryError 延迟重试；已有工具副作用时抑制错误文本，仅保留工具记录。
    RETRY_OR_SUPPRESS = "retry_or_suppress"


class PostInputStage(StrEnum):
    """输入落库之后共用的四段收尾阶段名（与 `conversation.turn_types.Stage` 后四段一致）。"""

    MODEL = "model"
    HISTORY_OUT = "history_out"
    DELIVERY = "delivery"
    FINALIZE = "finalize"


@dataclass(frozen=True, slots=True)
class EntryPolicy:
    """声明用户消息、定时任务、空闲跟进三个入口在模型与收尾阶段的策略差异。"""

    name: str
    runtime_error_policy: RuntimeErrorPolicy
    finalize_zero_balance_policy: ZeroBalancePolicy | None
    supports_completed_clear: bool
    send_typing_before_model: bool
    stream_visible_content: bool

    @property
    def suspend_if_zero(self) -> bool:
        return self.finalize_zero_balance_policy is ZeroBalancePolicy.SUSPEND_BOUNDARY


USER_ENTRY_POLICY = EntryPolicy(
    name="user",
    runtime_error_policy=RuntimeErrorPolicy.DELIVER_NOTICE,
    finalize_zero_balance_policy=ZeroBalancePolicy.SUSPEND_BOUNDARY,
    supports_completed_clear=True,
    send_typing_before_model=True,
    stream_visible_content=True,
)

SCHEDULE_ENTRY_POLICY = EntryPolicy(
    name="schedule",
    runtime_error_policy=RuntimeErrorPolicy.DELIVER_NOTICE,
    finalize_zero_balance_policy=None,
    supports_completed_clear=False,
    send_typing_before_model=True,
    stream_visible_content=True,
)

IDLE_FOLLOWUP_ENTRY_POLICY = EntryPolicy(
    name="idle_followup",
    runtime_error_policy=RuntimeErrorPolicy.RETRY_OR_SUPPRESS,
    finalize_zero_balance_policy=None,
    supports_completed_clear=False,
    send_typing_before_model=False,
    stream_visible_content=False,
)

ENTRY_POLICIES: dict[str, EntryPolicy] = {
    policy.name: policy
    for policy in (
        USER_ENTRY_POLICY,
        SCHEDULE_ENTRY_POLICY,
        IDLE_FOLLOWUP_ENTRY_POLICY,
    )
}


class ModelStepOutput(Protocol):
    @property
    def text(self) -> str: ...

    @property
    def tool_logs(self) -> list[ToolLog]: ...

    @property
    def status(self) -> LoopStatus | None: ...

    @property
    def turn_messages(self) -> list[dict[str, Any]] | None: ...


RawModelStepOutput = LoopResult | ModelStepOutput | tuple[str, list[ToolLog]]
StickerNormalizer = Callable[[str], Awaitable[str]]


@dataclass(frozen=True, slots=True)
class PreparedModelTurn:
    """`model` 阶段的统一产物：规范化回复、历史条目与流式已发送消息。"""

    text: str
    tool_logs: list[ToolLog]
    turn_messages: list[dict[str, Any]] | None
    runtime_error: str | None
    tool_record_entries: list[tuple[str, object]]
    reply_entries: list[tuple[str, object]]
    completed_clear: bool
    visible_sent_messages: tuple[Any, ...] = ()


class StageWrapper(Protocol):
    def __call__[T](
        self,
        stage: PostInputStage,
        step: Callable[[], Awaitable[T]],
    ) -> Awaitable[T]: ...


@dataclass(frozen=True, slots=True)
class PostInputTurnOutcome[DeliveryResultT]:
    """`execute_post_input_turn` 的执行结果。"""

    prepared: PreparedModelTurn
    delivered: bool
    delivery_result: DeliveryResultT | None = None


def _coerce_model_output(
    raw: RawModelStepOutput,
    *,
    classify_text: Callable[[str], str | None],
) -> tuple[str, list[ToolLog], LoopStatus | None, list[dict[str, Any]] | None]:
    if isinstance(raw, LoopResult):
        return raw.text, raw.tool_logs, raw.status, raw.turn_messages
    if isinstance(raw, tuple):
        coerced = LoopResult.coerce(raw, classify_text=classify_text)
        return coerced.text, coerced.tool_logs, coerced.status, coerced.turn_messages
    return (
        str(raw.text),
        raw.tool_logs,
        raw.status,
        raw.turn_messages,
    )


async def prepare_model_turn(
    *,
    policy: EntryPolicy,
    invoke_model: Callable[[VisibleContentSink | None], Awaitable[RawModelStepOutput]],
    normalize_stickers: StickerNormalizer,
    logger: logging.Logger,
    user_id: int | None = None,
    send_typing: Callable[[], Awaitable[Any]] | None = None,
    make_visible_handler: Callable[[], VisibleContentSink] | None = None,
    classify_error_text: Callable[[str], str | None] = runtime_error_cause,
    typing_debug_message: str = "Failed to send typing action before AI request",
) -> PreparedModelTurn:
    """执行主模型、规范化回复，并按 `EntryPolicy` 处理运行时错误与历史条目。"""
    if policy.send_typing_before_model and send_typing is not None:
        try:
            await send_typing()
        except Exception:
            logger.debug(typing_debug_message)

    visible_handler = (
        make_visible_handler()
        if policy.stream_visible_content and make_visible_handler is not None
        else None
    )

    with suppress_telegram_history():
        raw_output = await invoke_model(visible_handler)

    raw_text, tool_logs, status, turn_messages = _coerce_model_output(
        raw_output,
        classify_text=classify_error_text,
    )
    visible_sent = tuple(getattr(visible_handler, "sent_messages", ()))
    reply = await finish_reply(
        raw_text,
        normalize_stickers=normalize_stickers,
        status=status,
    )

    effective_text = reply.text
    if (
        reply.runtime_error
        and policy.runtime_error_policy is RuntimeErrorPolicy.RETRY_OR_SUPPRESS
    ):
        if not has_side_effects(tool_logs):
            raise NoSideEffectsRetryError(
                f"main AI failed during {policy.name}: {reply.runtime_error}"
            )
        logger.warning(
            "Background AI failed after tool execution; suppressing error notice: "
            "entry=%s user_id=%s cause=%s",
            policy.name,
            user_id,
            reply.runtime_error,
        )
        effective_text = ""

    return PreparedModelTurn(
        text=effective_text,
        tool_logs=tool_logs,
        turn_messages=turn_messages,
        runtime_error=reply.runtime_error,
        tool_record_entries=turn_record_entries(turn_messages, tool_logs),
        reply_entries=reply_record_entries(effective_text, reply.runtime_error),
        completed_clear=policy.supports_completed_clear and tool_logs_completed_clear(tool_logs),
        visible_sent_messages=visible_sent,
    )


async def execute_post_input_turn[DeliveryResultT](
    *,
    policy: EntryPolicy,
    run_model_step: Callable[[], Awaitable[PreparedModelTurn]],
    persist_output: Callable[[PreparedModelTurn], Awaitable[None]],
    deliver_output: Callable[[PreparedModelTurn], Awaitable[DeliveryResultT]],
    finalize_turn: Callable[[PreparedModelTurn, DeliveryResultT | None], Awaitable[None]]
    | None = None,
    before_persist_and_deliver: Callable[[PreparedModelTurn], Awaitable[bool]] | None = None,
    wrap_stage: StageWrapper | None = None,
) -> PostInputTurnOutcome[DeliveryResultT]:
    """按统一顺序运行 `model -> history_out -> delivery -> finalize`。

    `before_persist_and_deliver` 用于后台任务的投递前阶段推进或门闸检查（例如定时任务推进到
    `delivering`，或空闲跟进发现用户已回归时只补写工具记录并中止投递）。返回 `False` 时不再进入
    `history_out`、`delivery` 与 `finalize`。
    """

    async def _run_stage[T](stage: PostInputStage, step: Callable[[], Awaitable[T]]) -> T:
        if wrap_stage is None:
            return await step()
        return await wrap_stage(stage, step)

    prepared = await _run_stage(PostInputStage.MODEL, run_model_step)
    if before_persist_and_deliver is not None and not await before_persist_and_deliver(prepared):
        return PostInputTurnOutcome(prepared=prepared, delivered=False)

    await _run_stage(PostInputStage.HISTORY_OUT, lambda: persist_output(prepared))
    delivery_result = await _run_stage(
        PostInputStage.DELIVERY,
        lambda: deliver_output(prepared),
    )
    if finalize_turn is not None:
        await _run_stage(
            PostInputStage.FINALIZE,
            lambda: finalize_turn(prepared, delivery_result),
        )
    return PostInputTurnOutcome(
        prepared=prepared,
        delivered=True,
        delivery_result=delivery_result,
    )


def tool_log_type_labels(tool_logs: Sequence[ToolLog]) -> list[str]:
    return [
        str(tool_log.get("type", "tool_result"))
        for tool_log in tool_logs
        if isinstance(tool_log, dict)
    ]


__all__ = [
    "ENTRY_POLICIES",
    "EntryPolicy",
    "IDLE_FOLLOWUP_ENTRY_POLICY",
    "ModelStepOutput",
    "NoSideEffectsRetryError",
    "PostInputStage",
    "PostInputTurnOutcome",
    "PreparedModelTurn",
    "RawModelStepOutput",
    "RuntimeErrorPolicy",
    "SCHEDULE_ENTRY_POLICY",
    "StageWrapper",
    "USER_ENTRY_POLICY",
    "execute_post_input_turn",
    "prepare_model_turn",
    "tool_log_type_labels",
]
