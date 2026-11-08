from collections.abc import Awaitable, Callable, Iterable
from typing import Any

ToolLog = dict[str, Any]
AIResponse = tuple[str, list[ToolLog]]
# 向用户即时发送一段可见内容；在事件循环里直接 await，返回实际发出的文本。
VisibleContentHandler = Callable[[str], Awaitable[str | None]]

# 工具 handler 可用此内部字段把系统生成的上下文消息交回工具循环。
# 该字段不会暴露给模型。
TOOL_CONTEXT_MESSAGES_KEY = "_context_messages"

# 后台任务（定时任务、空闲跟进）在 tool_context 里放一个 threading.Event：
# 任务失去 claim 或超过执行上限时置位，工具循环在下一个检查点放弃剩余的模型调用和工具执行。
# 工具循环与可见内容发送都在事件循环里，运行中的 await 由任务取消终止；这个事件继续在每个检查点
# （模型调用前、工具执行前、发送前）阻止后续动作。线程里已经开始的同步工具不检查它，无法撤回。
ABORT_EVENT_KEY = "abort_event"

# 工具结果日志上的标记：生成的媒体即时发送到一半被截止时间打断，是否送达未知。
MEDIA_DELIVERY_KEY = "media_delivery"
MEDIA_DELIVERY_UNKNOWN = "unknown"


def media_delivery_attempted(tool_log: dict[str, Any]) -> bool:
    """这条工具结果的媒体已经即时发送过，或发送时被打断（是否送达未知）：投递阶段都不能再发一次。"""
    return bool(tool_log.get("media_sent")) or (
        tool_log.get(MEDIA_DELIVERY_KEY) == MEDIA_DELIVERY_UNKNOWN
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


def raise_if_aborted(tool_context: dict[str, Any] | None) -> None:
    event = (tool_context or {}).get(ABORT_EVENT_KEY)
    is_set = getattr(event, "is_set", None)
    if callable(is_set) and is_set():
        raise JobAbortedError("background job was aborted")


class PartialAIResponseError(Exception):
    def __init__(self, message: str, tool_logs: list[ToolLog]) -> None:
        super().__init__(message)
        self.tool_logs = list(tool_logs)


class TurnDeadlineError(PartialAIResponseError):
    """整轮截止时间到期（或进程停止）时，工具循环带着已有的工具日志退出。

    继承 `PartialAIResponseError`：工具已经执行过的部分不重试、不换 provider，
    只是 router 把它转成「超时」提示而不是「工具执行后回复生成失败」。
    `phase` 是到期时正在等待的步骤：`model`、`tool`、`delivery`。
    """

    def __init__(self, reason: str, phase: str, tool_logs: list[ToolLog]) -> None:
        super().__init__(f"turn deadline reached during {phase} ({reason})", tool_logs)
        self.reason = reason
        self.phase = phase
