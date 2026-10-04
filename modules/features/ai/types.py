from typing import Any, Callable, Dict, List, Tuple

ToolLog = Dict[str, Any]
AIResponse = Tuple[str, List[ToolLog]]
VisibleContentHandler = Callable[[str], str | None]

# 工具 handler 可用此内部字段把系统生成的上下文消息交回工具循环。
# 该字段不会暴露给模型。
TOOL_CONTEXT_MESSAGES_KEY = "_context_messages"

# 后台任务（定时任务、空闲跟进）在 tool_context 里放一个 threading.Event：
# 任务失去 claim 或超过执行上限时置位，工具循环在下一个检查点放弃剩余的模型调用和工具执行。
ABORT_EVENT_KEY = "abort_event"


class JobAbortedError(BaseException):
    """后台任务已被撤销，线程里的工具循环应当立刻退出。

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
