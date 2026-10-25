"""入群验证：开启了验证的群（`group_verification`）与每一轮验证（`verification_tasks`）。

`verification_tasks` 每个 (用户, 群组) 一行，记录当前这一轮验证的欢迎消息与到期时间（本地时间，naive）。
认领、到期与恢复的规则在 `features/moderation/operations/member_verify.py`；这里是单条语句级别的读写。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_telegram_bot.core import sql


@dataclass(frozen=True, slots=True)
class VerificationTask:
    user_id: int
    chat_id: int
    message_id: int


async def is_enabled(chat_id: int, *, connection: AsyncConnection | None = None) -> bool:
    row = await sql.fetch_one(
        "SELECT group_id FROM group_verification WHERE group_id = %s",
        (chat_id,),
        connection=connection,
    )
    return row is not None


async def enable(connection: AsyncConnection, chat_id: int, group_name: str) -> None:
    await connection.exec_driver_sql(
        "INSERT INTO group_verification (group_id, group_name) "
        "VALUES (%s, %s) "
        "ON DUPLICATE KEY UPDATE group_name = VALUES(group_name)",
        (chat_id, group_name),
    )


async def disable(connection: AsyncConnection, chat_id: int) -> None:
    await connection.exec_driver_sql(
        "DELETE FROM group_verification WHERE group_id = %s",
        (chat_id,),
    )


async def count_groups(*, connection: AsyncConnection | None = None) -> int:
    """开启了验证的群数。"""
    row = await sql.fetch_one("SELECT COUNT(*) FROM group_verification", connection=connection)
    return int(row[0]) if row else 0


async def list_group_ids(
    limit: int | None = None, *, connection: AsyncConnection | None = None
) -> list[int]:
    """开启了验证的群，最多 `limit` 个（None 为全部），不排序。"""
    suffix, params = ("", None) if limit is None else (" LIMIT %s", (limit,))
    rows = await sql.fetch_all(
        "SELECT group_id FROM group_verification" + suffix, params, connection=connection
    )
    return [int(row[0]) for row in rows]


async def save_task(
    connection: AsyncConnection,
    user_id: int,
    chat_id: int,
    message_id: int,
    expire_time: datetime,
) -> None:
    """记下这一轮验证；同一个 (用户, 群组) 已有记录时换成这一轮。"""
    await connection.exec_driver_sql(
        "INSERT INTO verification_tasks (user_id, group_id, message_id, expire_time) "
        "VALUES (%s, %s, %s, %s) "
        "ON DUPLICATE KEY UPDATE message_id = VALUES(message_id), expire_time = VALUES(expire_time)",
        (user_id, chat_id, message_id, expire_time),
    )


async def delete_task(
    connection: AsyncConnection, user_id: int, chat_id: int, message_id: int | None
) -> bool:
    """删掉这一轮验证的记录；记录已经不在、或已经换成别的欢迎消息时返回 False。"""
    result = await connection.exec_driver_sql(
        "DELETE FROM verification_tasks WHERE user_id = %s AND group_id = %s AND message_id = %s",
        (user_id, chat_id, message_id),
    )
    return result.rowcount > 0


async def delete_group_tasks(connection: AsyncConnection, chat_id: int) -> None:
    await connection.exec_driver_sql(
        "DELETE FROM verification_tasks WHERE group_id = %s",
        (chat_id,),
    )


async def find_task_message(
    user_id: int, chat_id: int, *, connection: AsyncConnection | None = None
) -> int | None:
    """这个成员当前这一轮验证的欢迎消息；没有在验证时返回 None。"""
    row = await sql.fetch_one(
        "SELECT message_id FROM verification_tasks WHERE user_id = %s AND group_id = %s",
        (user_id, chat_id),
        connection=connection,
    )
    return int(row[0]) if row else None


async def list_due_tasks(
    now: datetime, *, connection: AsyncConnection | None = None
) -> list[VerificationTask]:
    rows = await sql.fetch_all(
        "SELECT user_id, group_id, message_id FROM verification_tasks WHERE expire_time <= %s",
        (now,),
        connection=connection,
    )
    return [VerificationTask(int(row[0]), int(row[1]), int(row[2])) for row in rows]
