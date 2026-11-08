from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar

_REQUEST_CONTEXT: ContextVar[dict[str, object]] = ContextVar(
    "tool_request_context",
    default={},
)


def set_tool_request_context(context: dict[str, object] | None = None) -> None:
    _REQUEST_CONTEXT.set(context or {})


def clear_tool_request_context() -> None:
    _REQUEST_CONTEXT.set({})


def get_tool_request_context() -> dict[str, object]:
    return _REQUEST_CONTEXT.get()


@contextmanager
def tool_request_scope(context: Mapping[str, object] | None) -> Iterator[dict[str, object]]:
    """作用域里工具读到的请求上下文是 `context` 的一份副本，退出时恢复原来的上下文。

    每次模型尝试各用一份：工具在上下文里记的每请求次数、沙箱与命令去重缓存不会带到下一次尝试。
    """
    token = _REQUEST_CONTEXT.set(dict(context or {}))
    try:
        yield _REQUEST_CONTEXT.get()
    finally:
        _REQUEST_CONTEXT.reset(token)


__all__ = [
    "set_tool_request_context",
    "clear_tool_request_context",
    "get_tool_request_context",
    "tool_request_scope",
]
