"""群的垃圾信息过滤开关（`group_spam_control`）：总开关、链接过滤、@提及过滤。

开关的规则与缓存在 `features/moderation/operations/spam_control.py`；这里是单条语句级别的读写。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_telegram_bot.core import sql


@dataclass(frozen=True, slots=True)
class SpamSettings:
    enabled: bool
    block_links: bool
    block_mentions: bool


async def get_settings(
    chat_id: int, *, connection: AsyncConnection | None = None
) -> SpamSettings | None:
    """群的开关；从没设置过返回 None。"""
    row = await sql.fetch_one(
        "SELECT enabled, block_links, block_mentions FROM group_spam_control WHERE group_id = %s",
        (chat_id,),
        connection=connection,
    )
    if row is None:
        return None
    return SpamSettings(enabled=bool(row[0]), block_links=bool(row[1]), block_mentions=bool(row[2]))


async def save_enabled(
    connection: AsyncConnection,
    chat_id: int,
    *,
    enabled: bool,
    block_links: bool,
    block_mentions: bool,
    user_id: int,
) -> None:
    """写入总开关并记下操作的管理员；群还没有记录时按给定的链接与 @提及过滤新建。"""
    await connection.exec_driver_sql(
        "INSERT INTO group_spam_control (group_id, enabled, block_links, block_mentions, enabled_by) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON DUPLICATE KEY UPDATE enabled = %s, enabled_by = %s, updated_at = CURRENT_TIMESTAMP",
        (chat_id, enabled, block_links, block_mentions, user_id, enabled, user_id),
    )


async def set_block_links(connection: AsyncConnection, chat_id: int, enabled: bool) -> None:
    await connection.exec_driver_sql(
        "UPDATE group_spam_control SET block_links = %s, updated_at = CURRENT_TIMESTAMP "
        "WHERE group_id = %s",
        (enabled, chat_id),
    )


async def set_block_mentions(connection: AsyncConnection, chat_id: int, enabled: bool) -> None:
    await connection.exec_driver_sql(
        "UPDATE group_spam_control SET block_mentions = %s, updated_at = CURRENT_TIMESTAMP "
        "WHERE group_id = %s",
        (enabled, chat_id),
    )
