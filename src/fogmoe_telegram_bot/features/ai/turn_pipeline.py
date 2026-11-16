"""AI 对话三个入口（用户消息、定时任务、空闲跟进）在输入落库之后共用的执行流水线。

输入阶段各自不同（用户消息需要算费、扣费、识别图片；定时任务与空闲跟进需要管理租约与回顾事件），
但进入主模型之后的四段生命周期是一致的：

    model -> (可选：投递前门闸/阶段推进) -> history_out -> delivery -> finalize

入口之间的合理差异通过 `EntryPolicy` 显式声明，避免在三处复制同一套抑制历史、整理回复、
推导工具记录、处理运行时错误和零余额边界的逻辑。

模型阶段以 `LoopStatus.ABORTED` 结束（后台任务已被撤销）时，之后的阶段一律跳过：不写历史、不投递、
不收尾，也不按运行时错误安排重试。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from fogmoe_telegram_bot.core.telegram_history import suppress_telegram_history
from fogmoe_telegram_bot.features.ai.outbound import finish_reply
from fogmoe_telegram_bot.features.ai.tool_history import (
    completed_clear,
    reply_record_entries,
    turn_messages_to_record_entries,
)
from fogmoe_telegram_bot.features.ai.types import (
    LoopResult,
    LoopStatus,
    ToolCallRecord,
    VisibleContentHandler,
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
    # 收尾时余额已归零就在历史里补一条 `suspended` 事件（只有会扣费的用户轮次需要）。
    suspend_if_zero: bool
    supports_completed_clear: bool
    send_typing_before_model: bool
    stream_visible_content: bool


USER_ENTRY_POLICY = EntryPolicy(
    name="user",
    runtime_error_policy=RuntimeErrorPolicy.DELIVER_NOTICE,
    suspend_if_zero=True,
    supports_completed_clear=True,
    send_typing_before_model=True,
    stream_visible_content=True,
)

SCHEDULE_ENTRY_POLICY = EntryPolicy(
    name="schedule",
    runtime_error_policy=RuntimeErrorPolicy.DELIVER_NOTICE,
    suspend_if_zero=False,
    supports_completed_clear=False,
    send_typing_before_model=True,
    stream_visible_content=True,
)

IDLE_FOLLOWUP_ENTRY_POLICY = EntryPolicy(
    name="idle_followup",
    runtime_error_policy=RuntimeErrorPolicy.RETRY_OR_SUPPRESS,
    suspend_if_zero=False,
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


StickerNormalizer = Callable[[str], Awaitable[str]]


@dataclass(frozen=True, slots=True)
class PreparedModelTurn:
    """`model` 阶段的统一产物：规范化回复、历史条目与流式已发送消息。"""

    text: str
    status: LoopStatus
    tool_calls: tuple[ToolCallRecord, ...]
    turn_messages: list[dict[str, Any]]
    runtime_error: str | None
    tool_record_entries: list[tuple[str, object]]
    reply_entries: list[tuple[str, object]]
    completed_clear: bool
    has_side_effects: bool
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


async def prepare_model_turn(
    *,
    policy: EntryPolicy,
    invoke_model: Callable[[VisibleContentHandler | None], Awaitable[LoopResult]],
    normalize_stickers: StickerNormalizer,
    logger: logging.Logger,
    user_id: int | None = None,
    send_typing: Callable[[], Awaitable[Any]] | None = None,
    make_visible_handler: Callable[[], VisibleContentHandler] | None = None,
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
        output = await invoke_model(visible_handler)

    visible_sent = tuple(visible_handler.sent_messages) if visible_handler is not None else ()
    if output.status is LoopStatus.ABORTED:
        return PreparedModelTurn(
            text="",
            status=output.status,
            tool_calls=output.tool_calls,
            turn_messages=[],
            runtime_error=output.runtime_error,
            tool_record_entries=[],
            reply_entries=[],
            completed_clear=False,
            has_side_effects=output.has_side_effects,
            visible_sent_messages=visible_sent,
        )

    reply = await finish_reply(
        output.text,
        normalize_stickers=normalize_stickers,
        status=output.status,
    )

    effective_text = reply.text
    if (
        reply.runtime_error
        and policy.runtime_error_policy is RuntimeErrorPolicy.RETRY_OR_SUPPRESS
    ):
        if not output.has_side_effects:
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
        status=output.status,
        tool_calls=output.tool_calls,
        turn_messages=output.turn_messages,
        runtime_error=reply.runtime_error,
        tool_record_entries=turn_messages_to_record_entries(output.turn_messages),
        reply_entries=reply_record_entries(effective_text, reply.runtime_error),
        completed_clear=policy.supports_completed_clear and completed_clear(output.tool_calls),
        has_side_effects=output.has_side_effects,
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
    `history_out`、`delivery` 与 `finalize`。模型阶段以 `ABORTED` 结束时连这个检查也不做。
    """

    async def _run_stage[T](stage: PostInputStage, step: Callable[[], Awaitable[T]]) -> T:
        if wrap_stage is None:
            return await step()
        return await wrap_stage(stage, step)

    prepared = await _run_stage(PostInputStage.MODEL, run_model_step)
    if prepared.status is LoopStatus.ABORTED:
        return PostInputTurnOutcome(prepared=prepared, delivered=False)
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


def tool_call_labels(tool_calls: Sequence[ToolCallRecord]) -> list[str]:
    """日志里描述这一轮工具调用的标签：`工具名:状态`。"""
    return [f"{record.tool_name}:{record.status.value}" for record in tool_calls]


__all__ = [
    "ENTRY_POLICIES",
    "EntryPolicy",
    "IDLE_FOLLOWUP_ENTRY_POLICY",
    "NoSideEffectsRetryError",
    "PostInputStage",
    "PostInputTurnOutcome",
    "PreparedModelTurn",
    "RuntimeErrorPolicy",
    "SCHEDULE_ENTRY_POLICY",
    "StageWrapper",
    "USER_ENTRY_POLICY",
    "execute_post_input_turn",
    "prepare_model_turn",
    "tool_call_labels",
]
