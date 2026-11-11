"""AI 工具循环与任务边界的共享类型。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

ToolLog = dict[str, Any]
AIResponse = tuple[str, list[ToolLog]]


@runtime_checkable
class VisibleContentHandler(Protocol):
    """向用户即时发送可见文本与工具生成媒体的处理器协议。"""

    sent_messages: list[Any]

    async def __call__(self, content: str) -> str | None: ...

    async def send_tool_media(
        self,
        tool_name: str,
        result: dict[str, Any],
    ) -> list[Any]: ...

    def visible_events(self) -> list[dict[str, str]]: ...


# 测试里允许直接传普通异步函数 `async def handler(text) -> str | None`。
VisibleContentCallback = Callable[[str], Awaitable[str | None]]
VisibleContentSink = VisibleContentHandler | VisibleContentCallback


class LoopStatus(StrEnum):
    """一次工具循环或路由调用的结束状态。"""

    COMPLETED = "completed"
    TOOL_LIMIT_REACHED = "tool_limit_reached"
    PARTIAL_FAILURE = "partial_failure"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    SHUTDOWN = "shutdown"
    CONTEXT_BUDGET_EXCEEDED = "context_budget_exceeded"
    ALL_PROVIDERS_FAILED = "all_providers_failed"

    def runtime_error_cause(self) -> str | None:
        if self is LoopStatus.PARTIAL_FAILURE:
            return "partial_ai_response_failed"
        if self is LoopStatus.ALL_PROVIDERS_FAILED:
            return "all_ai_services_failed"
        if self is LoopStatus.CONTEXT_BUDGET_EXCEEDED:
            return "context_budget_exceeded"
        if self is LoopStatus.DEADLINE_EXCEEDED:
            return "turn_deadline_exceeded"
        if self is LoopStatus.SHUTDOWN:
            return "turn_interrupted_by_shutdown"
        return None


_CAUSE_TO_STATUS: Mapping[str, LoopStatus] = {
    "partial_ai_response_failed": LoopStatus.PARTIAL_FAILURE,
    "all_ai_services_failed": LoopStatus.ALL_PROVIDERS_FAILED,
    "context_budget_exceeded": LoopStatus.CONTEXT_BUDGET_EXCEEDED,
    "turn_deadline_exceeded": LoopStatus.DEADLINE_EXCEEDED,
    "turn_interrupted_by_shutdown": LoopStatus.SHUTDOWN,
}


def loop_status_from_cause(cause: str | None) -> LoopStatus:
    if not cause:
        return LoopStatus.COMPLETED
    return _CAUSE_TO_STATUS.get(cause, LoopStatus.COMPLETED)


class MediaDeliveryState(StrEnum):
    """媒体生成工具产物的即时投递状态。"""

    NOT_APPLICABLE = "not_applicable"
    PENDING = "pending"
    SENT = "sent"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class LoopResult:
    """工具循环与主聊天路由的类型化结果。

    同时兼容旧式的二元组解包 `text, tool_logs = result` 与下标访问，方便渐进迁移和单测断言；
    生产调用链直接读取 `status`、`runtime_error` 与 `turn_messages`，不再靠比对回复文案判错。
    """

    text: str
    tool_logs: list[ToolLog] = field(default_factory=list)
    status: LoopStatus = LoopStatus.COMPLETED
    turn_messages: list[dict[str, Any]] | None = None
    error: Exception | None = None

    @property
    def runtime_error(self) -> str | None:
        return self.status.runtime_error_cause()

    def __iter__(self) -> Iterator[Any]:
        yield self.text
        yield self.tool_logs

    def __len__(self) -> int:
        return 2

    def __getitem__(self, index: int) -> Any:
        return (self.text, self.tool_logs)[index]

    def __eq__(self, other: object) -> bool:
        if isinstance(other, LoopResult):
            return (
                self.text == other.text
                and self.tool_logs == other.tool_logs
                and self.status == other.status
                and self.turn_messages == other.turn_messages
            )
        if isinstance(other, tuple) and len(other) == 2:
            return (self.text, self.tool_logs) == other
        return NotImplemented

    @classmethod
    def coerce(
        cls,
        value: LoopResult | tuple[str, list[ToolLog]],
        *,
        classify_text: Callable[[str], str | None] | None = None,
    ) -> LoopResult:
        if isinstance(value, LoopResult):
            return value
        text, tool_logs = value
        cause = classify_text(text) if classify_text is not None else None
        return cls(
            text=text,
            tool_logs=list(tool_logs),
            status=loop_status_from_cause(cause),
            turn_messages=None,
        )


# 工具 handler 可用此内部字段把系统生成的上下文消息交回工具循环。
# 该字段不会暴露给模型。
TOOL_CONTEXT_MESSAGES_KEY = "_context_messages"

# 后台任务（定时任务、空闲跟进）在 tool_context 里放一个 threading.Event：
# 任务失去 claim 或超过执行上限时置位，工具循环在下一个检查点放弃剩余的模型调用和工具执行。
# 工具循环与可见内容发送都在事件循环里，运行中的 await 由任务取消终止；这个事件继续在每个检查点
# （模型调用前、工具执行前、发送前）阻止后续动作。线程里已经开始的同步工具不检查它，无法撤回。
ABORT_EVENT_KEY = "abort_event"

# 工具结果日志上的媒体投递状态字段名与未知状态常量。
MEDIA_DELIVERY_KEY = "media_delivery"
MEDIA_DELIVERY_UNKNOWN = MediaDeliveryState.UNKNOWN.value


def media_delivery_state(tool_log: Mapping[str, Any]) -> MediaDeliveryState:
    raw = tool_log.get(MEDIA_DELIVERY_KEY)
    if isinstance(raw, MediaDeliveryState):
        return raw
    if raw == MediaDeliveryState.UNKNOWN.value:
        return MediaDeliveryState.UNKNOWN
    if raw == MediaDeliveryState.SENT.value or bool(tool_log.get("media_sent")):
        return MediaDeliveryState.SENT
    if raw == MediaDeliveryState.PENDING.value:
        return MediaDeliveryState.PENDING
    return MediaDeliveryState.NOT_APPLICABLE


def media_delivery_attempted(tool_log: Mapping[str, Any]) -> bool:
    """这条工具结果的媒体已经即时发送过，或发送时被打断（是否送达未知）：投递阶段都不能再发一次。"""
    return media_delivery_state(tool_log) in (
        MediaDeliveryState.SENT,
        MediaDeliveryState.UNKNOWN,
    )


_SIDE_EFFECT_LOG_TYPES = frozenset(
    {"assistant_tool_call", "tool_result", "assistant_visible", "telegram_event"}
)


def has_side_effects(tool_logs: Iterable[ToolLog]) -> bool:
    """这一轮是否已经执行过工具或向用户发出过内容。

    「还能不能换模型、换 provider 从头重跑这一轮」只由它判断：工具循环据此把之后的异常包成
    `PartialAIResponseError` 并在原地重试模型调用，空闲跟进据此决定失败之后能不能重试。
    工具循环每次执行工具、发出可见内容都会先记一条日志，所以日志就是这一轮的副作用记录。
    """
    return any(log.get("type", "tool_result") in _SIDE_EFFECT_LOG_TYPES for log in tool_logs)


def delivered_to_user(tool_logs: Iterable[ToolLog]) -> bool:
    """这一轮是否已经有内容到达用户：可见文字，或即时发出（发送时被打断也算）的生成媒体。"""
    return any(
        (log.get("type") == "assistant_visible" and bool(str(log.get("content") or "").strip()))
        or media_delivery_attempted(log)
        for log in tool_logs
    )


class JobAbortedError(BaseException):
    """后台任务已被撤销，工具循环（以及线程里的同步工具）应当立刻退出。

    继承 BaseException：router 的 `except Exception` 会把普通异常当成 provider 失败、
    累计熔断并换下一个 provider 重跑；撤销不是 provider 的错，也不该触发这些路径。
    """


def raise_if_aborted(tool_context: Mapping[str, Any] | None) -> None:
    event = (tool_context or {}).get(ABORT_EVENT_KEY)
    is_set = getattr(event, "is_set", None)
    if callable(is_set) and is_set():
        raise JobAbortedError("background job was aborted")


class PartialAIResponseError(Exception):
    def __init__(
        self,
        message: str,
        tool_logs: list[ToolLog],
        *,
        turn_messages: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.tool_logs = list(tool_logs)
        self.turn_messages = list(turn_messages) if turn_messages is not None else None


class TurnDeadlineError(PartialAIResponseError):
    """整轮截止时间到期（或进程停止）时，工具循环带着已有的工具日志退出。

    继承 `PartialAIResponseError`：工具已经执行过的部分不重试、不换 provider，
    只是 router 把它转成「超时」提示而不是「工具执行后回复生成失败」。
    `phase` 是到期时正在等待的步骤：`model`、`tool`、`delivery`。
    """

    def __init__(
        self,
        reason: str,
        phase: str,
        tool_logs: list[ToolLog],
        *,
        turn_messages: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(
            f"turn deadline reached during {phase} ({reason})",
            tool_logs,
            turn_messages=turn_messages,
        )
        self.reason = reason
        self.phase = phase
