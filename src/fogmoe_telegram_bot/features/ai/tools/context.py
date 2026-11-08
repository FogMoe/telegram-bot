import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar

from ..types import ABORT_EVENT_KEY

_REQUEST_CONTEXT: ContextVar[dict[str, object]] = ContextVar(
    "tool_request_context",
    default={},
)


def build_tool_context(
    *,
    user_id: int,
    user_state_prompt: str,
    chat_id: int | None = None,
    chat_type: str | None = None,
    chat_title: str | None = None,
    message_id: int | None = None,
    message_edit_stamp: int | None = None,
    username: str | None = None,
    first_name: str | None = None,
    language_code: str | None = None,
    abort_event: threading.Event | None = None,
) -> dict[str, object]:
    """一轮 AI 对话的工具上下文；用户消息、定时任务、空闲跟进都用它构造，键只在这里定义。

    用户消息带上聊天、消息与发送者（代执行命令、群聊上下文工具要用）；定时任务与空闲跟进只有用户，
    聊天字段都是 None，并带上撤销信号 `abort_event`（`ABORT_EVENT_KEY`）。
    """
    is_group = chat_type in ("group", "supergroup")
    context: dict[str, object] = {
        "is_group": is_group,
        "group_id": chat_id if is_group else None,
        "chat_id": chat_id,
        "chat_type": chat_type,
        "chat_title": chat_title,
        "message_id": message_id,
        # 编辑过的消息会开始新的一轮；代执行命令的身份因此与编辑前的那一轮不同。
        "message_edit_stamp": message_edit_stamp,
        "user_id": user_id,
        "username": username,
        "first_name": first_name,
        "language_code": language_code,
        "user_state_prompt": user_state_prompt,
    }
    if abort_event is not None:
        context[ABORT_EVENT_KEY] = abort_event
    return context


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
    "build_tool_context",
    "set_tool_request_context",
    "clear_tool_request_context",
    "get_tool_request_context",
    "tool_request_scope",
]
