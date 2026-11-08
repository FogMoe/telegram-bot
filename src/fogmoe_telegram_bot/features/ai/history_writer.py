"""定时任务与空闲跟进写对话历史的共用入口。

每次写入之后：因为超出保留数而淘汰的归档快照打包发给用户；活跃历史溢出时先尝试即时摘要
（来不及时退回后台排队）；只生成了新快照时后台排队摘要。用户消息的一轮由
`ConversationTurn._persist_records` 按同样的规则处理，另外还会提示容量。
"""

import logging
from typing import Any

from fogmoe_telegram_bot.core import mysql_connection
from fogmoe_telegram_bot.core.archive_utils import send_permanent_records_archive

from . import summary


async def write_history(
    conversation_id: int,
    records: list[tuple[str, object]],
    *,
    bot: Any,
    logger: logging.Logger,
    **options: Any,
) -> None:
    """写入一批历史记录（`options` 原样交给 `async_insert_chat_records`）；没有记录时什么都不做。"""
    if not records:
        return
    snapshot_created, warning_level, archived_records = (
        await mysql_connection.async_insert_chat_records(
            conversation_id,
            records,
            **options,
        )
    )
    if archived_records:
        await send_permanent_records_archive(
            bot,
            conversation_id,
            archived_records,
            logger=logger,
        )
    if warning_level == "overflow":
        await summary.refresh_summary_after_overflow(conversation_id)
    elif snapshot_created:
        summary.schedule_summary_generation(conversation_id)
