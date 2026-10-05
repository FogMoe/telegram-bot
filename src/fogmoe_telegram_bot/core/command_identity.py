"""命令消息作为业务操作身份（op_key）时用的那一段。

以命令消息为身份的操作（/give、/bribe、/swap、/stake、/tl、/pic、RPG 等）用
`<chat_id>:<message_id>` 做 op_key，同一条命令被重复投递时不会再执行一次。

AI 代用户执行的命令复用触发这一轮对话的真实消息 ID（handler 的回复才能引用那条消息），
所以同一轮里的多次代执行共用一个消息 ID。代执行时 `delegated_operation()` 给出这一次操作
自己的身份，`message_identity()` 把它接在消息身份后面：不同的代执行命令得到不同的 op_key，
同一条代执行命令重放（同一轮对话、同样的命令文本）得到相同的 op_key。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_DELEGATED_OPERATION: ContextVar[str | None] = ContextVar(
    "delegated_operation",
    default=None,
)


@contextmanager
def delegated_operation(operation_id: str) -> Iterator[None]:
    """在这个作用域里执行的命令属于 `operation_id` 这次代执行。"""
    token = _DELEGATED_OPERATION.set(operation_id)
    try:
        yield
    finally:
        _DELEGATED_OPERATION.reset(token)


def message_identity(chat_id: int, message_id: int) -> tuple[object, ...]:
    """命令消息的身份：用户亲自发出时是 `(chat_id, message_id)`，代执行时再接上 `ai:<operation_id>`。"""
    operation_id = _DELEGATED_OPERATION.get()
    if operation_id is None:
        return (chat_id, message_id)
    return (chat_id, message_id, "ai", operation_id)
