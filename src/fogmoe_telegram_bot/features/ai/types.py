"""AI 工具循环与任务边界的共享类型。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class VisibleContentHandler(Protocol):
    """向用户即时发送可见文本与工具生成媒体的处理器。

    `sent_contents` 是这一轮里实际发出去的文字（按调用累计，部分发出时只记发出的部分），
    `sent_messages` 是发出的 Telegram 消息。工具循环据此判断一次调用发出了什么。
    """

    sent_messages: list[Any]
    sent_contents: list[str]

    async def __call__(self, content: str) -> str:
        """发送 `content`，返回实际发出的文字；规范化后没有可发的内容时返回空串。"""
        ...

    async def send_tool_media(
        self,
        tool_name: str,
        result: dict[str, Any],
    ) -> list[Any]: ...


class LoopStatus(StrEnum):
    """一次工具循环或路由调用的结束状态。"""

    COMPLETED = "completed"
    TOOL_LIMIT_REACHED = "tool_limit_reached"
    PARTIAL_FAILURE = "partial_failure"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    SHUTDOWN = "shutdown"
    CONTEXT_BUDGET_EXCEEDED = "context_budget_exceeded"
    ALL_PROVIDERS_FAILED = "all_providers_failed"
    # provider 以安全策略拒绝，且声明了不换下一个 provider：回退链停在这里。
    SAFETY_BLOCKED = "safety_blocked"
    # 后台任务失去 claim 或超时被撤销：之后不写历史、不投递、不收尾。
    ABORTED = "aborted"

    def runtime_error_cause(self) -> str | None:
        """固定错误提示的原因；None 表示 `text` 是 AI 的回复。"""
        return _RUNTIME_ERROR_CAUSES.get(self)


_RUNTIME_ERROR_CAUSES: dict[LoopStatus, str] = {
    LoopStatus.PARTIAL_FAILURE: "partial_ai_response_failed",
    LoopStatus.ALL_PROVIDERS_FAILED: "all_ai_services_failed",
    LoopStatus.CONTEXT_BUDGET_EXCEEDED: "context_budget_exceeded",
    LoopStatus.DEADLINE_EXCEEDED: "turn_deadline_exceeded",
    LoopStatus.SHUTDOWN: "turn_interrupted_by_shutdown",
    LoopStatus.SAFETY_BLOCKED: "ai_safety_blocked",
    LoopStatus.ABORTED: "job_aborted",
}


class ToolCallStatus(StrEnum):
    """一次工具调用的执行状态。"""

    # 已经开始处理，还没有结果；循环因异常离开时可能停在这里。
    RUNNING = "running"
    # 处理函数返回了结果（结果里可能带 error）。
    COMPLETED = "completed"
    # 缺函数名、工具未开放或未知、参数校验失败：没有运行处理函数，结果是错误说明。
    REJECTED = "rejected"
    # 运行中整轮截止时间到期，结果未知。
    INTERRUPTED = "interrupted"
    # 整轮截止时间到期，这次调用没来得及运行。
    NOT_EXECUTED = "not_executed"


class MediaDeliveryState(StrEnum):
    """媒体生成工具产物的即时投递状态。"""

    NOT_APPLICABLE = "not_applicable"
    # 已生成、还没发出：投递阶段发送。
    PENDING = "pending"
    SENT = "sent"
    # 发送时整轮被打断，是否送达未知：投递阶段也不再发。
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ToolCallRecord:
    """工具循环里一次工具调用的记录。

    `arguments` 是校验后的参数（校验失败时是原始参数），`public_result` 是交回模型的结果，
    `internal_result` 是处理函数返回的原始结果（媒体文件引用等只在这里）。
    """

    tool_name: str
    tool_call_id: str
    arguments: Any
    status: ToolCallStatus = ToolCallStatus.RUNNING
    public_result: Any = None
    internal_result: Any = None
    media_delivery: MediaDeliveryState = MediaDeliveryState.NOT_APPLICABLE

    @property
    def media_delivery_attempted(self) -> bool:
        """媒体已经即时发送过，或发送时被打断（是否送达未知）：投递阶段都不能再发一次。"""
        return self.media_delivery in (MediaDeliveryState.SENT, MediaDeliveryState.UNKNOWN)


@dataclass(frozen=True, slots=True)
class LoopResult:
    """工具循环与主聊天路由的类型化结果。

    - `text`：待投递的回复；即时发送过的内容不在这里。`status` 有固定错误原因时它是错误提示。
    - `tool_calls`：每次工具调用的记录。
    - `delivered_contents`：已经即时发给用户的可见文字。
    - `turn_messages`：本轮在循环里追加的消息序列（已配对），历史直接从它写入。
    """

    text: str
    status: LoopStatus = LoopStatus.COMPLETED
    tool_calls: tuple[ToolCallRecord, ...] = ()
    delivered_contents: tuple[str, ...] = ()
    turn_messages: list[dict[str, Any]] = field(default_factory=list)
    error: BaseException | None = field(default=None, compare=False)

    @property
    def runtime_error(self) -> str | None:
        return self.status.runtime_error_cause()

    @property
    def has_side_effects(self) -> bool:
        """这一轮是否已经开始执行工具或向用户发出过内容。

        「还能不能换模型、换 provider 从头重跑这一轮」只由它判断：工具循环据此把之后的异常包成
        `PartialAIResponseError` 并在原地重试模型调用，空闲跟进据此决定失败之后能不能重试。
        """
        return bool(self.tool_calls or self.delivered_contents)

    @property
    def delivered_to_user(self) -> bool:
        """这一轮是否已经有内容到达用户：可见文字，或即时发出（发送时被打断也算）的生成媒体。"""
        return any(content.strip() for content in self.delivered_contents) or any(
            record.media_delivery_attempted for record in self.tool_calls
        )


# 工具 handler 可用此内部字段把系统生成的上下文消息交回工具循环。
# 该字段不会暴露给模型。
TOOL_CONTEXT_MESSAGES_KEY = "_context_messages"

# 后台任务（定时任务、空闲跟进）在 tool_context 里放一个 threading.Event：
# 任务失去 claim 或超过执行上限时置位，工具循环在下一个检查点放弃剩余的模型调用和工具执行。
# 工具循环与可见内容发送都在事件循环里，运行中的 await 由任务取消终止；这个事件继续在每个检查点
# （模型调用前、工具执行前、发送前）阻止后续动作。线程里已经开始的同步工具不检查它，无法撤回。
ABORT_EVENT_KEY = "abort_event"


class JobAbortedError(BaseException):
    """后台任务已被撤销，工具循环（以及线程里的同步工具）应当立刻退出。

    继承 BaseException：回退执行器的 `except Exception` 会把普通异常当成 provider 失败、
    累计熔断并换下一个 provider 重跑；撤销不是 provider 的错，也不该触发这些路径。
    主聊天路由在边界上把它转成 `LoopStatus.ABORTED`。
    """


def raise_if_aborted(tool_context: Mapping[str, Any] | None) -> None:
    event = (tool_context or {}).get(ABORT_EVENT_KEY)
    is_set = getattr(event, "is_set", None)
    if callable(is_set) and is_set():
        raise JobAbortedError("background job was aborted")


class PartialAIResponseError(Exception):
    """执行过工具或发出过可见内容之后失败：`partial` 是到失败为止的循环结果，不能再从头重跑。"""

    def __init__(self, message: str, partial: LoopResult) -> None:
        super().__init__(message)
        self.partial = partial


class TurnDeadlineError(PartialAIResponseError):
    """整轮截止时间到期（或进程停止）时，工具循环带着已有的结果退出。

    继承 `PartialAIResponseError`：工具已经执行过的部分不重试、不换 provider，
    只是 router 把它转成「超时」提示而不是「工具执行后回复生成失败」。
    `phase` 是到期时正在等待的步骤：`model`、`tool`、`delivery`、`fallback`。
    """

    def __init__(self, reason: str, phase: str, partial: LoopResult) -> None:
        super().__init__(f"turn deadline reached during {phase} ({reason})", partial)
        self.reason = reason
        self.phase = phase
