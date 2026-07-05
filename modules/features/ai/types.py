from typing import Any, Callable, Dict, List, Tuple

ToolLog = Dict[str, Any]
AIResponse = Tuple[str, List[ToolLog]]
VisibleContentHandler = Callable[[str], str | None]


class PartialAIResponseError(Exception):
    def __init__(self, message: str, tool_logs: List[ToolLog]) -> None:
        super().__init__(message)
        self.tool_logs = list(tool_logs)
