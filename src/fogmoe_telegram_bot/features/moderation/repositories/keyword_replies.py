"""群的关键词自动回复（`group_keywords`）：触发词与回复内容（Telegram HTML）。

数量上限、HTML 净化、匹配与缓存在 `features/moderation/operations/keyword_replies.py`；这里是单条语句级别的读写。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_telegram_bot.core import sql


@dataclass(frozen=True, slots=True)
class KeywordReply:
    keyword: str
    response: str


async def list_replies(
    chat_id: int, *, connection: AsyncConnection | None = None
) -> list[KeywordReply]:
    rows = await sql.fetch_all(
        "SELECT keyword, response FROM group_keywords WHERE group_id = %s",
        (chat_id,),
        connection=connection,
    )
    return [KeywordReply(str(row[0]), str(row[1])) for row in rows]


async def reply_exists(
    chat_id: int, keyword: str, *, connection: AsyncConnection | None = None
) -> bool:
    row = await sql.fetch_one(
        "SELECT keyword FROM group_keywords WHERE group_id = %s AND keyword = %s",
        (chat_id, keyword),
        connection=connection,
    )
    return row is not None


async def count_replies(chat_id: int, *, connection: AsyncConnection | None = None) -> int:
    row = await sql.fetch_one(
        "SELECT COUNT(*) FROM group_keywords WHERE group_id = %s",
        (chat_id,),
        connection=connection,
    )
    return int(row[0]) if row else 0


async def upsert_reply(
    connection: AsyncConnection, chat_id: int, keyword: str, response: str, *, user_id: int
) -> None:
    """新增关键词回复；同一个触发词已经存在时更新回复内容与添加人。"""
    await connection.exec_driver_sql(
        "INSERT INTO group_keywords (group_id, keyword, response, created_by) "
        "VALUES (%s, %s, %s, %s) "
        "ON DUPLICATE KEY UPDATE response = VALUES(response), created_by = VALUES(created_by)",
        (chat_id, keyword, response, user_id),
    )


async def delete_reply(connection: AsyncConnection, chat_id: int, keyword: str) -> bool:
    """删掉关键词回复；没有这个触发词时返回 False。"""
    result = await connection.exec_driver_sql(
        "DELETE FROM group_keywords WHERE group_id = %s AND keyword = %s",
        (chat_id, keyword),
    )
    return result.rowcount > 0
