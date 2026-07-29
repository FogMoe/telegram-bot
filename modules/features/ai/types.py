from typing import Any, Callable, Dict, List, Tuple

ToolLog = Dict[str, Any]
AIResponse = Tuple[str, List[ToolLog]]
VisibleContentHandler = Callable[[str], str | None]

# 工具 handler 可用此内部字段把系统生成的上下文消息交回工具循环。
# 该字段不会暴露给模型。
TOOL_CONTEXT_MESSAGES_KEY = "_context_messages"


class PartialAIResponseError(Exception):
    def __init__(self, message: str, tool_logs: List[ToolLog]) -> None:
        super().__init__(message)
        self.tool_logs = list(tool_logs)
