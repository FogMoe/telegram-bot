from typing import Any, Awaitable, Callable, Dict, List, Tuple

ToolLog = Dict[str, Any]
AIResponse = Tuple[str, List[ToolLog]]
# 向用户即时发送一段可见内容；在事件循环里直接 await，返回实际发出的文本。
VisibleContentHandler = Callable[[str], Awaitable[str | None]]

# 工具 handler 可用此内部字段把系统生成的上下文消息交回工具循环。
# 该字段不会暴露给模型。
TOOL_CONTEXT_MESSAGES_KEY = "_context_messages"

# 后台任务（定时任务、空闲跟进）在 tool_context 里放一个 threading.Event：
# 任务失去 claim 或超过执行上限时置位，工具循环在下一个检查点放弃剩余的模型调用和工具执行。
# 工具循环与可见内容发送都在事件循环里，运行中的 await 由任务取消终止；这个事件继续负责
# 线程里的同步工具（它们读取同一个事件）以及发送前的检查。
ABORT_EVENT_KEY = "abort_event"


class JobAbortedError(BaseException):
    """后台任务已被撤销，工具循环（以及线程里的同步工具）应当立刻退出。

    继承 BaseException：router 的 `except Exception` 会把普通异常当成 provider 失败、
    累计熔断并换下一个 provider 重跑；撤销不是 provider 的错，也不该触发这些路径。
    """


def raise_if_aborted(tool_context: Dict[str, Any] | None) -> None:
    event = (tool_context or {}).get(ABORT_EVENT_KEY)
    is_set = getattr(event, "is_set", None)
    if callable(is_set) and is_set():
        raise JobAbortedError("background job was aborted")


class PartialAIResponseError(Exception):
    def __init__(self, message: str, tool_logs: List[ToolLog]) -> None:
        super().__init__(message)
        self.tool_logs = list(tool_logs)


class TurnDeadlineError(PartialAIResponseError):
    """整轮截止时间到期（或进程停止）时，工具循环带着已有的工具日志退出。

    继承 `PartialAIResponseError`：工具已经执行过的部分不重试、不换 provider，
    只是 router 把它转成「超时」提示而不是「工具执行后回复生成失败」。
    `phase` 是到期时正在等待的步骤：`model`、`tool`、`delivery`。
    """

    def __init__(self, reason: str, phase: str, tool_logs: List[ToolLog]) -> None:
        super().__init__(f"turn deadline reached during {phase} ({reason})", tool_logs)
        self.reason = reason
        self.phase = phase
