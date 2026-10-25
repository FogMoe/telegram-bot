"""群自定义的垃圾词（`group_spam_keywords`）：普通关键词或正则表达式。

数量上限、正则校验与缓存在 `features/moderation/operations/spam_control.py`；这里是单条语句级别的读写。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_telegram_bot.core import sql


@dataclass(frozen=True, slots=True)
class SpamKeyword:
    keyword: str  # 正则表达式不含 `//` 前缀
    is_regex: bool


async def list_keywords(
    chat_id: int, *, connection: AsyncConnection | None = None
) -> list[SpamKeyword]:
    rows = await sql.fetch_all(
        "SELECT keyword, is_regex FROM group_spam_keywords WHERE group_id = %s",
        (chat_id,),
        connection=connection,
    )
    return [SpamKeyword(str(row[0]), bool(row[1])) for row in rows]


async def keyword_exists(
    chat_id: int, keyword: str, *, connection: AsyncConnection | None = None
) -> bool:
    row = await sql.fetch_one(
        "SELECT id FROM group_spam_keywords WHERE group_id = %s AND keyword = %s",
        (chat_id, keyword),
        connection=connection,
    )
    return row is not None


async def count_keywords(chat_id: int, *, connection: AsyncConnection | None = None) -> int:
    row = await sql.fetch_one(
        "SELECT COUNT(*) FROM group_spam_keywords WHERE group_id = %s",
        (chat_id,),
        connection=connection,
    )
    return int(row[0]) if row else 0


async def upsert_keyword(
    connection: AsyncConnection, chat_id: int, keyword: str, *, is_regex: bool, user_id: int
) -> None:
    """新增垃圾词；同一个词已经存在时更新是否为正则与添加人。"""
    await connection.exec_driver_sql(
        "INSERT INTO group_spam_keywords (group_id, keyword, is_regex, created_by) "
        "VALUES (%s, %s, %s, %s) "
        "ON DUPLICATE KEY UPDATE is_regex = VALUES(is_regex), created_by = VALUES(created_by)",
        (chat_id, keyword, is_regex, user_id),
    )


async def delete_keyword(connection: AsyncConnection, chat_id: int, keyword: str) -> bool:
    """删掉垃圾词；没有这个词时返回 False。"""
    result = await connection.exec_driver_sql(
        "DELETE FROM group_spam_keywords WHERE group_id = %s AND keyword = %s",
        (chat_id, keyword),
    )
    return result.rowcount > 0
