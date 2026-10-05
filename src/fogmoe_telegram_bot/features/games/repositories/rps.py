"""石头剪刀布的对局（`rps_games`，迁移 0021）。

状态转换、加锁顺序与余额变动在 `features/games/rps_games.py`；这里是单条语句级别的读写。
截止时间一律用数据库时钟（`UTC_TIMESTAMP(6)`）比较，多个进程之间没有时钟偏差。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection

STATUS_CHOOSING = "choosing"
STATUS_SETTLED = "settled"  # 有结果：胜负已分（奖金已入账）或平局（入场费已退）
STATUS_REFUNDED = "refunded"  # 没有结果：超时或创建失败，入场费已退

# 终结了但结果还没写到面板上的对局，只在这个窗口内重试，之后放弃。
ANNOUNCE_RETRY_WINDOW = "1 DAY"

MESSAGE_COLUMNS = frozenset(
    {
        "p1_message_id",
        "p1_private_msg_id",
        "p2_message_id",
        "p2_private_msg_id",
    }
)


@dataclass(frozen=True, slots=True)
class Seat:
    user_id: int
    name: str
    chat_id: int
    message_id: int | None = None
    private_msg_id: int | None = None
    choice: str | None = None


@dataclass(frozen=True, slots=True)
class Game:
    id: int
    status: str
    outcome: str | None
    same_chat: bool
    p1: Seat
    p2: Seat
    seconds_left: float
    announced: bool

    @property
    def is_choosing(self) -> bool:
        return self.status == STATUS_CHOOSING

    def seat_of(self, user_id: int) -> Seat | None:
        if self.p1.user_id == user_id:
            return self.p1
        if self.p2.user_id == user_id:
            return self.p2
        return None

    def opponent_of(self, user_id: int) -> Seat | None:
        if self.p1.user_id == user_id:
            return self.p2
        if self.p2.user_id == user_id:
            return self.p1
        return None


_GAME_SELECT = (
    "SELECT id, status, outcome, same_chat, "
    "p1_id, p1_name, p1_chat_id, p1_message_id, p1_private_msg_id, p1_choice, "
    "p2_id, p2_name, p2_chat_id, p2_message_id, p2_private_msg_id, p2_choice, "
    "TIMESTAMPDIFF(MICROSECOND, UTC_TIMESTAMP(6), expires_at), announced_at IS NOT NULL "
    "FROM rps_games WHERE "
)


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _game_from_row(row: Row[Any]) -> Game:
    return Game(
        id=int(row[0]),
        status=str(row[1]),
        outcome=None if row[2] is None else str(row[2]),
        same_chat=bool(row[3]),
        p1=Seat(
            user_id=int(row[4]),
            name=str(row[5]),
            chat_id=int(row[6]),
            message_id=_optional_int(row[7]),
            private_msg_id=_optional_int(row[8]),
            choice=None if row[9] is None else str(row[9]),
        ),
        p2=Seat(
            user_id=int(row[10]),
            name=str(row[11]),
            chat_id=int(row[12]),
            message_id=_optional_int(row[13]),
            private_msg_id=_optional_int(row[14]),
            choice=None if row[15] is None else str(row[15]),
        ),
        seconds_left=int(row[16]) / 1_000_000,
        announced=bool(row[17]),
    )


async def load_game(
    connection: AsyncConnection, game_id: int, *, for_update: bool = False
) -> Game | None:
    suffix = " FOR UPDATE" if for_update else ""
    row = (await connection.exec_driver_sql(_GAME_SELECT + "id = %s" + suffix, (game_id,))).first()
    return None if row is None else _game_from_row(row)


async def find_choosing_game(connection: AsyncConnection, user_id: int) -> Game | None:
    """玩家当前所在的进行中对局（没有返回 None）。"""
    row = (
        await connection.exec_driver_sql(
            _GAME_SELECT + "status = %s AND (p1_id = %s OR p2_id = %s) ORDER BY id LIMIT 1",
            (STATUS_CHOOSING, user_id, user_id),
        )
    ).first()
    return None if row is None else _game_from_row(row)


async def has_choosing_game(connection: AsyncConnection, user_id: int) -> bool:
    row = (
        await connection.exec_driver_sql(
            "SELECT id FROM rps_games WHERE status = %s AND (p1_id = %s OR p2_id = %s) LIMIT 1",
            (STATUS_CHOOSING, user_id, user_id),
        )
    ).first()
    return row is not None


async def insert_game(
    connection: AsyncConnection, p1: Seat, p2: Seat, *, same_chat: bool, seconds: int
) -> int:
    """创建进行中的对局并返回 id。"""
    result = await connection.exec_driver_sql(
        "INSERT INTO rps_games (status, same_chat, "
        "p1_id, p1_name, p1_chat_id, p1_message_id, "
        "p2_id, p2_name, p2_chat_id, p2_message_id, created_at, expires_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
        "UTC_TIMESTAMP(6), UTC_TIMESTAMP(6) + INTERVAL %s SECOND)",
        (
            STATUS_CHOOSING,
            int(same_chat),
            p1.user_id,
            p1.name[:255],
            p1.chat_id,
            p1.message_id,
            p2.user_id,
            p2.name[:255],
            p2.chat_id,
            p2.message_id,
            seconds,
        ),
    )
    return int(result.lastrowid)


async def update_message_ids(connection: AsyncConnection, game_id: int, **columns: int) -> None:
    """记录发出去的面板消息 id（`p1_private_msg_id=...` 等），之后的编辑与恢复靠它们定位。"""
    if not columns or not set(columns) <= MESSAGE_COLUMNS:
        raise ValueError(f"不支持的列: {sorted(columns)}")
    assignments = ", ".join(f"{name} = %s" for name in columns)
    await connection.exec_driver_sql(
        f"UPDATE rps_games SET {assignments} WHERE id = %s",
        (*columns.values(), game_id),
    )


async def set_choice(
    connection: AsyncConnection, game_id: int, slot: Literal["p1", "p2"], choice: str
) -> None:
    column = "p1_choice" if slot == "p1" else "p2_choice"
    await connection.exec_driver_sql(
        f"UPDATE rps_games SET {column} = %s WHERE id = %s", (choice, game_id)
    )


async def finish_game(
    connection: AsyncConnection, game_id: int, *, status: str, outcome: str
) -> None:
    """choosing -> 终态：只有仍在进行中的对局会被改动。"""
    await connection.exec_driver_sql(
        "UPDATE rps_games SET status = %s, outcome = %s, finished_at = UTC_TIMESTAMP(6) "
        "WHERE id = %s AND status = %s",
        (status, outcome, game_id, STATUS_CHOOSING),
    )


async def due_game_ids(connection: AsyncConnection) -> list[int]:
    """已过期的进行中对局。"""
    rows = (
        await connection.exec_driver_sql(
            "SELECT id FROM rps_games WHERE status = %s AND expires_at <= UTC_TIMESTAMP(6) "
            "ORDER BY id",
            (STATUS_CHOOSING,),
        )
    ).all()
    return [int(row[0]) for row in rows]


async def unannounced_game_ids(connection: AsyncConnection) -> list[int]:
    """已终结但结果还没写到面板上的对局（只在 `ANNOUNCE_RETRY_WINDOW` 内）。"""
    rows = (
        await connection.exec_driver_sql(
            "SELECT id FROM rps_games WHERE status <> %s AND announced_at IS NULL "
            f"AND finished_at > UTC_TIMESTAMP(6) - INTERVAL {ANNOUNCE_RETRY_WINDOW} ORDER BY id",
            (STATUS_CHOOSING,),
        )
    ).all()
    return [int(row[0]) for row in rows]


async def mark_announced(connection: AsyncConnection, game_id: int) -> None:
    await connection.exec_driver_sql(
        "UPDATE rps_games SET announced_at = UTC_TIMESTAMP(6) "
        "WHERE id = %s AND announced_at IS NULL",
        (game_id,),
    )
