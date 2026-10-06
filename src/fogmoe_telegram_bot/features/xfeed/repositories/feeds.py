"""群组的 X 账号同步设置（`group_x_feeds`，迁移 0022）。

行存在即表示这个群付过开通费；`enabled` 为 0 表示已开通但暂停了同步（账号与进度保留）。
开通、换绑与轮询进度的规则在 `features/xfeed/operations.py`；这里是单条语句级别的读写。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.engine import Row
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_telegram_bot.core import sql


@dataclass(frozen=True, slots=True)
class GroupFeed:
    chat_id: int
    handle: str
    enabled: bool
    last_seen_id: int
    paid_by: int


@dataclass(frozen=True, slots=True)
class ActiveFeed:
    chat_id: int
    handle: str
    last_seen_id: int


def _feed_from_row(row: Row[Any]) -> GroupFeed:
    return GroupFeed(
        chat_id=int(row[0]),
        handle=str(row[1]),
        enabled=bool(row[2]),
        last_seen_id=int(row[3]),
        paid_by=int(row[4]),
    )


async def get_feed(
    chat_id: int,
    *,
    connection: AsyncConnection | None = None,
    for_update: bool = False,
) -> GroupFeed | None:
    """群的同步设置；没开通过返回 None。`for_update` 只用于已知存在的行。"""
    suffix = " FOR UPDATE" if for_update else ""
    row = await sql.fetch_one(
        "SELECT chat_id, x_handle, enabled, last_seen_id, paid_by FROM group_x_feeds "
        "WHERE chat_id = %s" + suffix,
        (chat_id,),
        connection=connection,
    )
    return None if row is None else _feed_from_row(row)


async def insert_feed(
    connection: AsyncConnection,
    *,
    chat_id: int,
    handle: str,
    last_seen_id: int,
    user_id: int,
    op_key: str,
) -> bool:
    """登记开通并绑定账号；这个群已经开通过（主键冲突）时返回 False，不改动已有行。"""
    try:
        await connection.exec_driver_sql(
            "INSERT INTO group_x_feeds "
            "(chat_id, x_handle, enabled, last_seen_id, bound_by, paid_by, paid_op_key, "
            "paid_at, updated_at) "
            "VALUES (%s, %s, 1, %s, %s, %s, %s, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6))",
            (chat_id, handle, last_seen_id, user_id, user_id, op_key),
        )
    except IntegrityError as exc:
        if sql.is_duplicate_key_error(exc):
            return False
        raise
    return True


async def set_binding(
    connection: AsyncConnection,
    chat_id: int,
    *,
    handle: str,
    last_seen_id: int,
    user_id: int,
) -> None:
    """绑定账号并启用同步。"""
    await connection.exec_driver_sql(
        "UPDATE group_x_feeds SET x_handle = %s, enabled = 1, last_seen_id = %s, bound_by = %s, "
        "updated_at = UTC_TIMESTAMP(6) WHERE chat_id = %s",
        (handle, last_seen_id, user_id, chat_id),
    )


async def disable(connection: AsyncConnection, chat_id: int, *, handle: str | None = None) -> bool:
    """暂停同步，账号与进度保留；给了 `handle` 时只在仍绑定着它时暂停。没有可暂停的返回 False。"""
    if handle is None:
        result = await connection.exec_driver_sql(
            "UPDATE group_x_feeds SET enabled = 0, updated_at = UTC_TIMESTAMP(6) "
            "WHERE chat_id = %s AND enabled = 1",
            (chat_id,),
        )
    else:
        result = await connection.exec_driver_sql(
            "UPDATE group_x_feeds SET enabled = 0, updated_at = UTC_TIMESTAMP(6) "
            "WHERE chat_id = %s AND enabled = 1 AND x_handle = %s",
            (chat_id, handle),
        )
    return result.rowcount == 1


async def list_active_feeds(*, connection: AsyncConnection | None = None) -> list[ActiveFeed]:
    rows = await sql.fetch_all(
        "SELECT chat_id, x_handle, last_seen_id FROM group_x_feeds "
        "WHERE enabled = 1 ORDER BY chat_id",
        connection=connection,
    )
    return [ActiveFeed(int(r[0]), str(r[1]), int(r[2])) for r in rows]


async def advance_last_seen(
    connection: AsyncConnection, chat_id: int, *, handle: str, last_seen_id: int
) -> bool:
    """推进同步进度；群已换绑或进度已经更新时不改动，返回 False。"""
    result = await connection.exec_driver_sql(
        "UPDATE group_x_feeds SET last_seen_id = %s, updated_at = UTC_TIMESTAMP(6) "
        "WHERE chat_id = %s AND x_handle = %s AND last_seen_id < %s",
        (last_seen_id, chat_id, handle, last_seen_id),
    )
    return result.rowcount == 1


async def move_chat(connection: AsyncConnection, old_chat_id: int, new_chat_id: int) -> bool:
    """群升级成超级群后把设置搬到新的 chat id；新 id 已有记录时返回 False，不改动。"""
    try:
        result = await connection.exec_driver_sql(
            "UPDATE group_x_feeds SET chat_id = %s, updated_at = UTC_TIMESTAMP(6) "
            "WHERE chat_id = %s",
            (new_chat_id, old_chat_id),
        )
    except IntegrityError as exc:
        if sql.is_duplicate_key_error(exc):
            return False
        raise
    return result.rowcount == 1
