"""多人下注的轮次与下注（`gamble_rounds`、`gamble_bets`，迁移 0021）。

状态转换与并发规则在 `features/games/gamble_rounds.py`；这里是单条语句级别的读写。
截止时间一律用数据库时钟（`UTC_TIMESTAMP(6)`）比较，多个进程之间没有时钟偏差。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.engine import Row
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_telegram_bot.core import sql

STATUS_OPEN = "open"
STATUS_SETTLED = "settled"
STATUS_REFUNDED = "refunded"
STATUS_CANCELLED = "cancelled"

# 终结了但还没把结果写到面板上的轮次，只在这个窗口内重试，之后放弃。
ANNOUNCE_RETRY_WINDOW = "1 DAY"


@dataclass(frozen=True, slots=True)
class Round:
    id: int
    chat_id: int
    message_id: int | None
    status: str
    seconds_left: float
    winner_id: int | None
    prize: int
    announced: bool

    @property
    def is_open(self) -> bool:
        return self.status == STATUS_OPEN


@dataclass(frozen=True, slots=True)
class Bet:
    user_id: int
    username: str
    amount: int
    op_key: str


_ROUND_SELECT = (
    "SELECT id, chat_id, message_id, status, "
    "TIMESTAMPDIFF(MICROSECOND, UTC_TIMESTAMP(6), closes_at), "
    "winner_id, prize, announced_at IS NOT NULL "
    "FROM gamble_rounds WHERE id = %s"
)


def _round_from_row(row: Row[Any]) -> Round:
    return Round(
        id=int(row[0]),
        chat_id=int(row[1]),
        message_id=None if row[2] is None else int(row[2]),
        status=str(row[3]),
        seconds_left=int(row[4]) / 1_000_000,
        winner_id=None if row[5] is None else int(row[5]),
        prize=int(row[6]),
        announced=bool(row[7]),
    )


async def load_round(
    connection: AsyncConnection, round_id: int, *, for_update: bool = False
) -> Round | None:
    suffix = " FOR UPDATE" if for_update else ""
    row = (await connection.exec_driver_sql(_ROUND_SELECT + suffix, (round_id,))).first()
    return None if row is None else _round_from_row(row)


async def load_bets(
    connection: AsyncConnection, round_id: int, *, for_update: bool = False
) -> tuple[Bet, ...]:
    suffix = " FOR UPDATE" if for_update else ""
    rows = (
        await connection.exec_driver_sql(
            "SELECT user_id, username, amount, op_key FROM gamble_bets "
            "WHERE round_id = %s ORDER BY id" + suffix,
            (round_id,),
        )
    ).all()
    return tuple(Bet(int(r[0]), str(r[1]), int(r[2]), str(r[3])) for r in rows)


async def insert_round(connection: AsyncConnection, chat_id: int, seconds: int) -> int | None:
    """开一个开放轮次并返回它的 id；已经有开放轮次时返回 None（`active_slot` 的唯一键兜底）。"""
    try:
        result = await connection.exec_driver_sql(
            "INSERT INTO gamble_rounds (chat_id, status, active_slot, created_at, closes_at) "
            "VALUES (%s, %s, 1, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6) + INTERVAL %s SECOND)",
            (chat_id, STATUS_OPEN, seconds),
        )
    except IntegrityError as exc:
        if sql.is_duplicate_key_error(exc):
            return None
        raise
    return int(result.lastrowid)


async def attach_message(connection: AsyncConnection, round_id: int, message_id: int) -> bool:
    """把面板消息记到还在等面板的开放轮次上；轮次已不是这个状态返回 False。"""
    result = await connection.exec_driver_sql(
        "UPDATE gamble_rounds SET message_id = %s "
        "WHERE id = %s AND status = %s AND message_id IS NULL",
        (message_id, round_id, STATUS_OPEN),
    )
    return result.rowcount == 1


async def cancel_round(connection: AsyncConnection, round_id: int) -> bool:
    """关掉还没有面板的开放轮次（没有 message_id 就不可能有人下注）；条件不符返回 False。"""
    result = await connection.exec_driver_sql(
        "UPDATE gamble_rounds SET status = %s, active_slot = NULL, "
        "settled_at = UTC_TIMESTAMP(6), announced_at = UTC_TIMESTAMP(6) "
        "WHERE id = %s AND status = %s AND message_id IS NULL",
        (STATUS_CANCELLED, round_id, STATUS_OPEN),
    )
    return result.rowcount == 1


async def insert_bet(
    connection: AsyncConnection,
    *,
    round_id: int,
    user_id: int,
    username: str,
    amount: int,
    op_key: str,
) -> bool:
    """登记一笔下注；`(round_id, user_id)` 已有下注时返回 False。"""
    try:
        await connection.exec_driver_sql(
            "INSERT INTO gamble_bets (round_id, user_id, username, amount, op_key, created_at) "
            "VALUES (%s, %s, %s, %s, %s, UTC_TIMESTAMP(6))",
            (round_id, user_id, username[:255], amount, op_key),
        )
    except IntegrityError as exc:
        if sql.is_duplicate_key_error(exc):
            return False
        raise
    return True


async def finish_round(
    connection: AsyncConnection,
    round_id: int,
    *,
    status: str,
    winner_id: int | None,
    prize: int,
) -> None:
    """open -> settled / refunded：只有仍是 open 的轮次会被改动。"""
    await connection.exec_driver_sql(
        "UPDATE gamble_rounds SET status = %s, active_slot = NULL, winner_id = %s, "
        "prize = %s, settled_at = UTC_TIMESTAMP(6) WHERE id = %s AND status = %s",
        (status, winner_id, prize, round_id, STATUS_OPEN),
    )


async def due_round_ids(connection: AsyncConnection) -> list[int]:
    """已过截止时间的开放轮次。"""
    rows = (
        await connection.exec_driver_sql(
            "SELECT id FROM gamble_rounds "
            "WHERE status = %s AND closes_at <= UTC_TIMESTAMP(6) ORDER BY id",
            (STATUS_OPEN,),
        )
    ).all()
    return [int(row[0]) for row in rows]


async def unannounced_round_ids(connection: AsyncConnection) -> list[int]:
    """已终结但结果还没写到面板上的轮次（只在 `ANNOUNCE_RETRY_WINDOW` 内）。"""
    rows = (
        await connection.exec_driver_sql(
            "SELECT id FROM gamble_rounds WHERE status <> %s AND announced_at IS NULL "
            f"AND settled_at > UTC_TIMESTAMP(6) - INTERVAL {ANNOUNCE_RETRY_WINDOW} ORDER BY id",
            (STATUS_OPEN,),
        )
    ).all()
    return [int(row[0]) for row in rows]


async def mark_announced(connection: AsyncConnection, round_id: int) -> None:
    await connection.exec_driver_sql(
        "UPDATE gamble_rounds SET announced_at = UTC_TIMESTAMP(6) "
        "WHERE id = %s AND announced_at IS NULL",
        (round_id,),
    )
