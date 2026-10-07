"""群的 AI 垃圾识别付费状态与成员检查计数（`group_spam_ai`、`group_spam_ai_members`，迁移 0023）。

付费、续期、暂停与提醒的规则在 `features/moderation/spam_ai/operations.py`；这里是单条语句级别的读写。
时间都是 UTC，数据库里是不带时区的 DATETIME(6)，读出来是 naive datetime。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy.engine import Row
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_telegram_bot.core import sql


@dataclass(frozen=True, slots=True)
class SpamAiGroup:
    chat_id: int
    enabled: bool
    paid_until: datetime  # UTC，naive
    active: bool  # 有效期还没过（按数据库时钟）


@dataclass(frozen=True, slots=True)
class DueReminder:
    chat_id: int
    paid_until: datetime


class ReminderKind(StrEnum):
    EXPIRING = "expiring"
    EXPIRED = "expired"


# 记录「已经为哪一个 paid_until 发过这种提醒」的列
_REMINDER_COLUMNS = {
    ReminderKind.EXPIRING: "reminded_soon_for",
    ReminderKind.EXPIRED: "reminded_expired_for",
}


def _group_from_row(row: Row[Any]) -> SpamAiGroup:
    return SpamAiGroup(
        chat_id=int(row[0]),
        enabled=bool(row[1]),
        paid_until=row[2],
        active=bool(row[3]),
    )


async def get_group(
    chat_id: int,
    *,
    connection: AsyncConnection | None = None,
    for_update: bool = False,
) -> SpamAiGroup | None:
    """群的付费状态；从没付过费返回 None。"""
    suffix = " FOR UPDATE" if for_update else ""
    row = await sql.fetch_one(
        "SELECT chat_id, enabled, paid_until, paid_until > UTC_TIMESTAMP(6) FROM group_spam_ai "
        "WHERE chat_id = %s" + suffix,
        (chat_id,),
        connection=connection,
    )
    return None if row is None else _group_from_row(row)


async def extend_period(connection: AsyncConnection, chat_id: int, *, user_id: int, days: int) -> None:
    """有效期在 `max(现在, paid_until)` 上加 `days` 天并启用；群还没有记录时新建。"""
    await connection.exec_driver_sql(
        "INSERT INTO group_spam_ai (chat_id, enabled, paid_until, last_paid_by, updated_at) "
        "VALUES (%s, 1, UTC_TIMESTAMP(6) + INTERVAL %s DAY, %s, UTC_TIMESTAMP(6)) "
        "ON DUPLICATE KEY UPDATE "
        "paid_until = GREATEST(paid_until, UTC_TIMESTAMP(6)) + INTERVAL %s DAY, "
        "enabled = 1, last_paid_by = %s, updated_at = UTC_TIMESTAMP(6)",
        (chat_id, days, user_id, days, user_id),
    )


async def resume(connection: AsyncConnection, chat_id: int) -> bool:
    """恢复检查；不在有效期内或本来就开着时返回 False。"""
    result = await connection.exec_driver_sql(
        "UPDATE group_spam_ai SET enabled = 1, updated_at = UTC_TIMESTAMP(6) "
        "WHERE chat_id = %s AND enabled = 0 AND paid_until > UTC_TIMESTAMP(6)",
        (chat_id,),
    )
    return result.rowcount == 1


async def pause(connection: AsyncConnection, chat_id: int) -> bool:
    """暂停检查，有效期不变；不在有效期内或本来就暂停着时返回 False。"""
    result = await connection.exec_driver_sql(
        "UPDATE group_spam_ai SET enabled = 0, updated_at = UTC_TIMESTAMP(6) "
        "WHERE chat_id = %s AND enabled = 1 AND paid_until > UTC_TIMESTAMP(6)",
        (chat_id,),
    )
    return result.rowcount == 1


async def list_due_reminders(
    kind: ReminderKind,
    *,
    within_days: int = 0,
    connection: AsyncConnection | None = None,
) -> list[DueReminder]:
    """开着检查、到了提醒时间、还没为当前 `paid_until` 发过这种提醒的群。

    EXPIRING：有效期还剩不到 `within_days` 天；EXPIRED：有效期已过。暂停中的群不提醒。
    """
    column = _REMINDER_COLUMNS[kind]
    if kind is ReminderKind.EXPIRING:
        window = (
            "paid_until > UTC_TIMESTAMP(6) AND paid_until <= UTC_TIMESTAMP(6) + INTERVAL %s DAY"
        )
        params: tuple[Any, ...] = (within_days,)
    else:
        window = "paid_until <= UTC_TIMESTAMP(6)"
        params = ()
    rows = await sql.fetch_all(
        f"SELECT chat_id, paid_until FROM group_spam_ai WHERE enabled = 1 AND {window} "
        f"AND NOT ({column} <=> paid_until) ORDER BY chat_id",
        params,
        connection=connection,
    )
    return [DueReminder(int(row[0]), row[1]) for row in rows]


async def claim_reminder(
    connection: AsyncConnection, kind: ReminderKind, chat_id: int, paid_until: datetime
) -> bool:
    """记下已经为这个 `paid_until` 发过提醒；别处先记下了、或有效期已经变了时返回 False。"""
    column = _REMINDER_COLUMNS[kind]
    result = await connection.exec_driver_sql(
        f"UPDATE group_spam_ai SET {column} = paid_until "
        f"WHERE chat_id = %s AND paid_until = %s AND NOT ({column} <=> paid_until)",
        (chat_id, paid_until),
    )
    return result.rowcount == 1


async def get_checked_count(
    chat_id: int, user_id: int, *, connection: AsyncConnection | None = None
) -> int:
    row = await sql.fetch_one(
        "SELECT checked_count FROM group_spam_ai_members WHERE chat_id = %s AND user_id = %s",
        (chat_id, user_id),
        connection=connection,
    )
    return 0 if row is None else int(row[0])


async def add_checked(connection: AsyncConnection, chat_id: int, user_id: int) -> int:
    """这个成员又有一条消息被判定为正常，返回累计条数。"""
    await connection.exec_driver_sql(
        "INSERT INTO group_spam_ai_members (chat_id, user_id, checked_count, updated_at) "
        "VALUES (%s, %s, 1, UTC_TIMESTAMP(6)) "
        "ON DUPLICATE KEY UPDATE checked_count = checked_count + 1, updated_at = UTC_TIMESTAMP(6)",
        (chat_id, user_id),
    )
    return await get_checked_count(chat_id, user_id, connection=connection)


async def move_chat(connection: AsyncConnection, old_chat_id: int, new_chat_id: int) -> bool:
    """把付费状态与检查计数搬到新的 chat id；旧 id 没有记录、或新 id 已经有记录时返回 False，不改动。"""
    try:
        result = await connection.exec_driver_sql(
            "UPDATE group_spam_ai SET chat_id = %s, updated_at = UTC_TIMESTAMP(6) WHERE chat_id = %s",
            (new_chat_id, old_chat_id),
        )
    except IntegrityError as exc:
        if sql.is_duplicate_key_error(exc):
            return False
        raise
    if result.rowcount != 1:
        return False
    # 新群里已经有计数的成员保留新群的那一行
    await connection.exec_driver_sql(
        "UPDATE IGNORE group_spam_ai_members SET chat_id = %s WHERE chat_id = %s",
        (new_chat_id, old_chat_id),
    )
    await connection.exec_driver_sql(
        "DELETE FROM group_spam_ai_members WHERE chat_id = %s", (old_chat_id,)
    )
    return True


async def forget_member(connection: AsyncConnection, chat_id: int, user_id: int) -> None:
    """清掉成员的检查计数，下次发言重新从头检查。"""
    await connection.exec_driver_sql(
        "DELETE FROM group_spam_ai_members WHERE chat_id = %s AND user_id = %s",
        (chat_id, user_id),
    )
