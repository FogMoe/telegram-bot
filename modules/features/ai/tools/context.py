from contextvars import ContextVar
from typing import Dict, Optional

_REQUEST_CONTEXT: ContextVar[Dict[str, object]] = ContextVar(
    "tool_request_context",
    default={},
)


def set_tool_request_context(context: Optional[Dict[str, object]] = None) -> None:
    _REQUEST_CONTEXT.set(context or {})


def clear_tool_request_context() -> None:
    _REQUEST_CONTEXT.set({})


def get_tool_request_context() -> Dict[str, object]:
    return _REQUEST_CONTEXT.get()


__all__ = [
    "set_tool_request_context",
    "clear_tool_request_context",
    "get_tool_request_context",
]
