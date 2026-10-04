"""石头剪刀布对局的持久化业务操作，不依赖 Telegram。

对局在 `rps_games`（迁移 0021）：两名玩家、双方面板消息、选择、状态与结果、创建/过期时间。
余额变动一律走 `core.balance`，op_key 由对局 id 派生：

- 入场扣款 `rps:<game_id>:entry:<uid>`，两名玩家的扣款与对局创建在**同一个事务**提交：
  任何一方余额不足或用户不存在，整个事务回滚，对局不存在，另一方也没有被扣款。
- 平局、超时、创建后消息发不出去：对原入场 op_key 调 `balance.refund`，只会退一次。
- 胜者奖金 `rps:<game_id>:win`。

状态转换只发生一次：所有转换都先 `SELECT ... FOR UPDATE` 锁对局行，状态必须仍是 choosing。
加锁顺序固定为「对局行 -> user 行（按 id 升序）」；创建对局时对局行还不存在，先按 id 升序
锁两个 user 行，所以并发的创建、结算不会互相死锁。完整的恢复策略见 docs/job-recovery.md
的「游戏状态」一节。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection

from core import balance, sql

logger = logging.getLogger(__name__)

ROCK = "rock"
PAPER = "paper"
SCISSORS = "scissors"
CHOICES = (ROCK, PAPER, SCISSORS)
_BEATS = {(ROCK, SCISSORS), (PAPER, ROCK), (SCISSORS, PAPER)}

ENTRY_FEE = 1
WIN_PAYOUT = 2
GAME_SECONDS = 120

STATUS_CHOOSING = "choosing"
STATUS_SETTLED = "settled"  # 有结果：胜负已分（奖金已入账）或平局（入场费已退）
STATUS_REFUNDED = "refunded"  # 没有结果：超时或创建失败，入场费已退

OUTCOME_P1 = "p1"
OUTCOME_P2 = "p2"
OUTCOME_DRAW = "draw"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_FAILED = "failed"

# 创建对局被拒绝的原因
CODE_INSUFFICIENT = "insufficient"
CODE_NO_USER = "no_user"
CODE_BUSY = "busy"  # 玩家已经在另一局里

# 提交选择的结果
CHOICE_RECORDED = "recorded"  # 已记录，等对方选择
CHOICE_SETTLED = "settled"  # 双方都选了，已结算
CHOICE_EXPIRED = "expired"  # 已超过过期时间，本次调用把对局按超时退款了
CHOICE_ALREADY = "already_chosen"
CHOICE_FINISHED = "finished"  # 对局早已结束
CHOICE_NOT_FOUND = "not_found"
CHOICE_NOT_PLAYER = "not_player"

# 终结了但结果还没写到面板上的对局，只在这个窗口内重试，之后放弃。
ANNOUNCE_RETRY_WINDOW = "1 DAY"


class StartRejected(Exception):
    """对局没有创建。抛出时事务已回滚：没有对局记录，两名玩家都没有被扣款。"""

    def __init__(self, code: str, user_id: int | None = None) -> None:
        super().__init__(f"对局创建被拒绝: {code} user={user_id}")
        self.code = code
        self.user_id = user_id


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


@dataclass(frozen=True, slots=True)
class ChoiceResult:
    code: str
    game: Game | None = None


def entry_op_key(game_id: int, user_id: int) -> str:
    return balance.make_op_key("rps", game_id, "entry", user_id)


def payout_op_key(game_id: int) -> str:
    return balance.make_op_key("rps", game_id, "win")


def decide(choice1: str, choice2: str) -> str:
    """胜负：返回 OUTCOME_P1 / OUTCOME_P2 / OUTCOME_DRAW。"""
    if choice1 == choice2:
        return OUTCOME_DRAW
    return OUTCOME_P1 if (choice1, choice2) in _BEATS else OUTCOME_P2


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


async def _load_game(
    connection: AsyncConnection, game_id: int, *, for_update: bool = False
) -> Game | None:
    suffix = " FOR UPDATE" if for_update else ""
    row = (await connection.exec_driver_sql(_GAME_SELECT + "id = %s" + suffix, (game_id,))).first()
    return None if row is None else _game_from_row(row)


async def load_game(game_id: int) -> Game | None:
    async with sql.connect() as connection:
        return await _load_game(connection, game_id)


async def active_game_for(user_id: int) -> Game | None:
    """玩家当前所在的进行中对局（没有返回 None）。"""
    async with sql.connect() as connection:
        row = (
            await connection.exec_driver_sql(
                _GAME_SELECT + "status = %s AND (p1_id = %s OR p2_id = %s) ORDER BY id LIMIT 1",
                (STATUS_CHOOSING, user_id, user_id),
            )
        ).first()
    return None if row is None else _game_from_row(row)


# ---------------------------------------------------------------------------
# 创建
# ---------------------------------------------------------------------------


async def create_game(
    p1: Seat, p2: Seat, *, same_chat: bool, seconds: int = GAME_SECONDS
) -> Game:
    """创建对局并扣两名玩家的入场费，同一个事务。

    p1 是等待中的玩家，p2 是加入者。任何一方余额不足、用户不存在或已经在另一局里，
    抛 `StartRejected`，事务回滚，没有对局也没有扣款。
    """

    if p1.user_id == p2.user_id:
        raise ValueError("对局需要两名不同的玩家")

    async def work(connection: AsyncConnection) -> Game:
        user_ids = sorted({p1.user_id, p2.user_id})
        for user_id in user_ids:
            try:
                await balance.lock_user(connection, user_id)
            except balance.UserNotFound as exc:
                raise StartRejected(CODE_NO_USER, user_id) from exc

        # 已经锁住两名玩家的 user 行：同一个玩家的并发创建在这里串行，
        # 这个事务的第一次一致性读发生在拿到锁之后，能看到上一个持锁者提交的对局。
        for user_id in user_ids:
            busy = (
                await connection.exec_driver_sql(
                    "SELECT id FROM rps_games WHERE status = %s AND (p1_id = %s OR p2_id = %s) "
                    "LIMIT 1",
                    (STATUS_CHOOSING, user_id, user_id),
                )
            ).first()
            if busy is not None:
                raise StartRejected(CODE_BUSY, user_id)

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
        game_id = int(result.lastrowid)
        for user_id in user_ids:
            try:
                await balance.debit(
                    connection,
                    user_id,
                    ENTRY_FEE,
                    op_key=entry_op_key(game_id, user_id),
                    reason="rps_entry",
                )
            except balance.InsufficientBalance as exc:
                raise StartRejected(CODE_INSUFFICIENT, user_id) from exc
            except balance.UserNotFound as exc:
                raise StartRejected(CODE_NO_USER, user_id) from exc
        created = await _load_game(connection, game_id)
        assert created is not None
        return created

    return await balance.run_in_transaction(work)


_MESSAGE_COLUMNS = frozenset(
    {
        "p1_message_id",
        "p1_private_msg_id",
        "p2_message_id",
        "p2_private_msg_id",
    }
)


async def record_message_ids(game_id: int, **columns: int) -> None:
    """记录发出去的面板消息 id（`p1_private_msg_id=...` 等），之后的编辑与恢复靠它们定位。"""
    if not columns or not set(columns) <= _MESSAGE_COLUMNS:
        raise ValueError(f"不支持的列: {sorted(columns)}")
    assignments = ", ".join(f"{name} = %s" for name in columns)
    await sql.execute(
        f"UPDATE rps_games SET {assignments} WHERE id = %s",
        (*columns.values(), game_id),
    )


# ---------------------------------------------------------------------------
# 终结（调用方已锁住对局行且状态是 choosing）
# ---------------------------------------------------------------------------


async def _refund_entries(connection: AsyncConnection, game: Game, *, reason: str) -> None:
    for user_id in sorted({game.p1.user_id, game.p2.user_id}):
        try:
            await balance.refund(connection, entry_op_key(game.id, user_id), reason=reason)
        except balance.UserNotFound:
            logger.error("退还入场费时用户不存在 game=%s user=%s", game.id, user_id)


async def _finish(
    connection: AsyncConnection, game_id: int, *, status: str, outcome: str
) -> Game:
    await connection.exec_driver_sql(
        "UPDATE rps_games SET status = %s, outcome = %s, finished_at = UTC_TIMESTAMP(6) "
        "WHERE id = %s AND status = %s",
        (status, outcome, game_id, STATUS_CHOOSING),
    )
    finished = await _load_game(connection, game_id)
    assert finished is not None
    return finished


async def _settle_locked(connection: AsyncConnection, game: Game) -> Game:
    """双方都已选择：平局退入场费，否则把奖金入账给胜者。"""
    assert game.p1.choice and game.p2.choice
    outcome = decide(game.p1.choice, game.p2.choice)
    if outcome == OUTCOME_DRAW:
        await _refund_entries(connection, game, reason="rps_draw")
        return await _finish(connection, game.id, status=STATUS_SETTLED, outcome=OUTCOME_DRAW)

    winner = game.p1 if outcome == OUTCOME_P1 else game.p2
    try:
        await balance.credit(
            connection,
            winner.user_id,
            WIN_PAYOUT,
            op_key=payout_op_key(game.id),
            reason="rps_win",
        )
    except balance.UserNotFound:
        logger.error("对局 %s 的胜者 %s 不存在，改为退还入场费", game.id, winner.user_id)
        await _refund_entries(connection, game, reason="rps_failed")
        return await _finish(connection, game.id, status=STATUS_REFUNDED, outcome=OUTCOME_FAILED)
    return await _finish(connection, game.id, status=STATUS_SETTLED, outcome=outcome)


async def _expire_locked(connection: AsyncConnection, game: Game) -> Game:
    await _refund_entries(connection, game, reason="rps_timeout")
    return await _finish(connection, game.id, status=STATUS_REFUNDED, outcome=OUTCOME_TIMEOUT)


# ---------------------------------------------------------------------------
# 选择、超时、取消
# ---------------------------------------------------------------------------


async def record_choice(game_id: int, user_id: int, choice: str) -> ChoiceResult:
    """记录一名玩家的选择；双方都选了就在同一个事务里结算。"""
    if choice not in CHOICES:
        return ChoiceResult(CHOICE_NOT_FOUND)

    async def work(connection: AsyncConnection) -> ChoiceResult:
        game = await _load_game(connection, game_id, for_update=True)
        if game is None:
            return ChoiceResult(CHOICE_NOT_FOUND)
        seat = game.seat_of(user_id)
        if seat is None:
            return ChoiceResult(CHOICE_NOT_PLAYER, game)
        if not game.is_choosing:
            return ChoiceResult(CHOICE_FINISHED, game)
        if game.seconds_left <= 0:
            return ChoiceResult(CHOICE_EXPIRED, await _expire_locked(connection, game))
        if seat.choice is not None:
            return ChoiceResult(CHOICE_ALREADY, game)

        column = "p1_choice" if game.p1.user_id == user_id else "p2_choice"
        await connection.exec_driver_sql(
            f"UPDATE rps_games SET {column} = %s WHERE id = %s", (choice, game_id)
        )
        updated = await _load_game(connection, game_id)
        assert updated is not None
        if updated.p1.choice and updated.p2.choice:
            return ChoiceResult(CHOICE_SETTLED, await _settle_locked(connection, updated))
        return ChoiceResult(CHOICE_RECORDED, updated)

    return await balance.run_in_transaction(work)


async def expire_game(game_id: int, *, only_if_due: bool = True) -> Game | None:
    """把超时的对局退款并标记。返回刚终结的对局；对局不存在、已终结或还没到期返回 None。"""

    async def work(connection: AsyncConnection) -> Game | None:
        game = await _load_game(connection, game_id, for_update=True)
        if game is None or not game.is_choosing:
            return None
        if only_if_due and game.seconds_left > 0:
            return None
        return await _expire_locked(connection, game)

    return await balance.run_in_transaction(work)


async def cancel_game(game_id: int) -> Game | None:
    """创建之后消息发不出去等失败：退还入场费并标记失败。返回刚终结的对局，已终结返回 None。"""

    async def work(connection: AsyncConnection) -> Game | None:
        game = await _load_game(connection, game_id, for_update=True)
        if game is None or not game.is_choosing:
            return None
        await _refund_entries(connection, game, reason="rps_failed")
        return await _finish(connection, game_id, status=STATUS_REFUNDED, outcome=OUTCOME_FAILED)

    return await balance.run_in_transaction(work)


async def expire_due_games() -> list[Game]:
    """退款所有已过期的进行中对局（重启后的恢复，也兜底丢失的定时器）。

    每局各自一个事务；某一局失败只记日志，下一轮再试。
    """
    async with sql.connect() as connection:
        rows = (
            await connection.exec_driver_sql(
                "SELECT id FROM rps_games WHERE status = %s AND expires_at <= UTC_TIMESTAMP(6) "
                "ORDER BY id",
                (STATUS_CHOOSING,),
            )
        ).all()
    expired: list[Game] = []
    for row in rows:
        game_id = int(row[0])
        try:
            game = await expire_game(game_id)
        except Exception:
            logger.exception("退款超时对局 %s 失败，稍后重试", game_id)
            continue
        if game is not None:
            expired.append(game)
    return expired


# ---------------------------------------------------------------------------
# 结果公告
# ---------------------------------------------------------------------------


async def unannounced_game_ids() -> list[int]:
    """已终结但结果还没写到面板上的对局（进程在终结提交之后、编辑面板之前退出的情形）。"""
    async with sql.connect() as connection:
        rows = (
            await connection.exec_driver_sql(
                "SELECT id FROM rps_games WHERE status <> %s AND announced_at IS NULL "
                f"AND finished_at > UTC_TIMESTAMP(6) - INTERVAL {ANNOUNCE_RETRY_WINDOW} ORDER BY id",
                (STATUS_CHOOSING,),
            )
        ).all()
    return [int(row[0]) for row in rows]


async def mark_announced(game_id: int) -> None:
    await sql.execute(
        "UPDATE rps_games SET announced_at = UTC_TIMESTAMP(6) "
        "WHERE id = %s AND announced_at IS NULL",
        (game_id,),
    )
