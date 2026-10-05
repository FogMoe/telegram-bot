"""多人下注的轮次、下注与结算：持久化的业务操作，不依赖 Telegram。

状态都在 `gamble_rounds` / `gamble_bets`（迁移 0021），进程重启不丢；读写这两张表的 SQL 在
`repositories/gamble.py`，这里持有事务与锁顺序。余额变动一律走 `core.balance`，op_key 由持久 id 派生：
`gamble:<round_id>:bet:<uid>`、`gamble:<round_id>:payout`。
完整的恢复策略见 docs/job-recovery.md 的「游戏状态」一节。

并发
----
- 轮次行是这一局的总开关：接受下注与结算都先 `SELECT ... FOR UPDATE` 锁轮次行，两者因此串行。
  持锁之后才确认状态是 open 且没过截止时间，随后写下注并扣款，任何一步失败整个事务回滚。
- 下注靠 `(round_id, user_id)` 唯一键兜底，同一玩家的并发下注只会成功一次。
- 加锁顺序固定为「轮次行 -> user 行」。
- 同一时间只有一个开放轮次由 `active_slot` 的唯一键保证：开放期间为 1，终结时置 NULL。
- 截止时间一律用数据库时钟（`UTC_TIMESTAMP(6)`）比较，多个进程之间没有时钟偏差。

结算只会发生一次：锁住轮次行后状态必须仍是 open，转换为 settled（有人下注就同事务入账奖金）
或 refunded（中奖者账户不存在时的兜底：全额退回各人的下注）。
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_telegram_bot.core import balance, sql

from .repositories import gamble as gamble_repository
from .repositories.gamble import ANNOUNCE_RETRY_WINDOW as ANNOUNCE_RETRY_WINDOW
from .repositories.gamble import STATUS_CANCELLED as STATUS_CANCELLED
from .repositories.gamble import STATUS_OPEN as STATUS_OPEN
from .repositories.gamble import STATUS_REFUNDED as STATUS_REFUNDED
from .repositories.gamble import STATUS_SETTLED as STATUS_SETTLED
from .repositories.gamble import Bet as Bet
from .repositories.gamble import Round as Round

logger = logging.getLogger(__name__)

ROUND_SECONDS = 300
BET_AMOUNTS = (5, 10, 20)

# 拒绝下注的原因，回调据此给出提示。
CODE_STALE = "stale"  # 轮次不存在、金额非法，或者按钮不在这一局的面板上
CODE_CLOSED = "closed"  # 轮次已停止接受下注
CODE_ALREADY_BET = "already_bet"
CODE_INSUFFICIENT = "insufficient"
CODE_NO_USER = "no_user"


class BetRejected(Exception):
    """下注被拒绝。抛出时事务已经回滚：没有扣款，也没有登记下注。"""

    def __init__(self, code: str) -> None:
        super().__init__(f"下注被拒绝: {code}")
        self.code = code


@dataclass(frozen=True, slots=True)
class Settlement:
    """一次结算调用的结果。`transitioned` 为 False 表示轮次早已终结，本次没有改动任何数据。"""

    round: Round
    bets: tuple[Bet, ...]
    transitioned: bool

    @property
    def winner(self) -> Bet | None:
        winner_id = self.round.winner_id
        return next((bet for bet in self.bets if bet.user_id == winner_id), None)


def bet_op_key(round_id: int, user_id: int) -> str:
    return balance.make_op_key("gamble", round_id, "bet", user_id)


def payout_op_key(round_id: int) -> str:
    return balance.make_op_key("gamble", round_id, "payout")


async def load_round(round_id: int) -> Round | None:
    async with sql.connect() as connection:
        return await gamble_repository.load_round(connection, round_id)


async def load_bets(round_id: int) -> tuple[Bet, ...]:
    async with sql.connect() as connection:
        return await gamble_repository.load_bets(connection, round_id)


# ---------------------------------------------------------------------------
# 开局
# ---------------------------------------------------------------------------


async def open_round(chat_id: int, *, seconds: int = ROUND_SECONDS) -> Round | None:
    """开一个新轮次；已经有开放轮次时返回 None（`active_slot` 的唯一键兜底）。

    新轮次还没有 message_id，面板发出去之后用 `attach_message` 补上；在那之前所有下注都会被拒绝。
    """

    async def work(connection: AsyncConnection) -> Round | None:
        round_id = await gamble_repository.insert_round(connection, chat_id, seconds)
        if round_id is None:
            return None
        return await gamble_repository.load_round(connection, round_id)

    return await balance.run_in_transaction(work)


async def attach_message(round_id: int, message_id: int) -> bool:
    """把发出去的面板消息记到轮次上。返回 False 表示轮次已不是等待面板的开放状态。"""
    async with sql.transaction() as connection:
        return await gamble_repository.attach_message(connection, round_id, message_id)


async def cancel_round(round_id: int) -> bool:
    """面板没能发出去：关掉还没有面板的轮次。没有 message_id 就不可能有人下注，所以无需退款。"""
    async with sql.transaction() as connection:
        return await gamble_repository.cancel_round(connection, round_id)


# ---------------------------------------------------------------------------
# 下注
# ---------------------------------------------------------------------------


async def accept_bet(
    round_id: int,
    *,
    chat_id: int | None,
    message_id: int | None,
    user_id: int,
    username: str,
    amount: int,
) -> Bet:
    """接受一笔下注：锁轮次 -> 校验 -> 登记下注 -> 扣款，同一个事务。

    `chat_id` / `message_id` 是点击的那个面板所在的位置，必须与轮次记录的面板一致。
    被拒绝时抛 `BetRejected`，事务回滚，没有扣款也没有登记。
    """
    if amount not in BET_AMOUNTS:
        raise BetRejected(CODE_STALE)

    async def work(connection: AsyncConnection) -> Bet:
        current = await gamble_repository.load_round(connection, round_id, for_update=True)
        if current is None:
            raise BetRejected(CODE_STALE)
        if (
            current.message_id is None
            or current.chat_id != chat_id
            or current.message_id != message_id
        ):
            raise BetRejected(CODE_STALE)
        if not current.is_open or current.seconds_left <= 0:
            raise BetRejected(CODE_CLOSED)

        op_key = bet_op_key(round_id, user_id)
        inserted = await gamble_repository.insert_bet(
            connection,
            round_id=round_id,
            user_id=user_id,
            username=username,
            amount=amount,
            op_key=op_key,
        )
        if not inserted:
            raise BetRejected(CODE_ALREADY_BET)
        try:
            await balance.debit(
                connection, user_id, amount, op_key=op_key, reason="gamble_bet"
            )
        except balance.InsufficientBalance as exc:
            raise BetRejected(CODE_INSUFFICIENT) from exc
        except balance.UserNotFound as exc:
            raise BetRejected(CODE_NO_USER) from exc
        return Bet(user_id, username[:255], amount, op_key)

    return await balance.run_in_transaction(work)


# ---------------------------------------------------------------------------
# 结算
# ---------------------------------------------------------------------------


def draw_winner(bets: tuple[Bet, ...], rng: random.Random | None = None) -> Bet:
    """按下注金额作为权重抽中奖者。"""
    rng = rng or random.Random()
    return rng.choices(bets, weights=[bet.amount for bet in bets], k=1)[0]


async def _refund_bets(connection: AsyncConnection, bets: tuple[Bet, ...]) -> None:
    for bet in bets:
        try:
            await balance.refund(connection, bet.op_key, reason="gamble_refund")
        except balance.UserNotFound:
            logger.error("退还下注时用户不存在 op_key=%s", bet.op_key)


async def settle_round(round_id: int, *, only_if_due: bool = False) -> Settlement | None:
    """结算一个轮次；轮次不存在返回 None。

    只有 open -> settled/refunded 这一次转换会改动数据，轮次已终结时原样返回当前状态
    （`transitioned=False`）。`only_if_due` 为 True 时，没过截止时间的轮次保持开放。
    """

    async def work(connection: AsyncConnection) -> Settlement | None:
        current = await gamble_repository.load_round(connection, round_id, for_update=True)
        if current is None:
            return None
        # 持有轮次行锁之后再读下注：下注的写入都要先拿这把锁，这里一定能看到全部已提交的下注。
        bets = await gamble_repository.load_bets(connection, round_id, for_update=True)
        if not current.is_open or (only_if_due and current.seconds_left > 0):
            return Settlement(current, bets, transitioned=False)

        status = STATUS_SETTLED
        winner: Bet | None = None
        prize = sum(bet.amount for bet in bets)
        if bets:
            winner = draw_winner(bets)
            try:
                await balance.credit(
                    connection,
                    winner.user_id,
                    prize,
                    op_key=payout_op_key(round_id),
                    reason="gamble_win",
                )
            except balance.UserNotFound:
                # 中奖者的账户已经不存在：不留下无人领取的奖池，全额退回各人的下注。
                logger.error("轮次 %s 的中奖者 %s 不存在，改为全额退款", round_id, winner.user_id)
                await _refund_bets(connection, bets)
                status = STATUS_REFUNDED
                winner = None
        await gamble_repository.finish_round(
            connection,
            round_id,
            status=status,
            winner_id=None if winner is None else winner.user_id,
            prize=0 if winner is None else prize,
        )
        settled = await gamble_repository.load_round(connection, round_id)
        assert settled is not None
        return Settlement(settled, bets, transitioned=True)

    return await balance.run_in_transaction(work)


async def settle_due_rounds() -> list[Settlement]:
    """结算所有已过截止时间的开放轮次（重启后的恢复，也兜底丢失的定时器）。

    每个轮次各自一个事务；某个轮次失败只记日志，下一轮再试，不影响其他轮次。
    """
    async with sql.connect() as connection:
        round_ids = await gamble_repository.due_round_ids(connection)
    settlements: list[Settlement] = []
    for round_id in round_ids:
        try:
            settlement = await settle_round(round_id, only_if_due=True)
        except Exception:
            logger.exception("结算轮次 %s 失败，稍后重试", round_id)
            continue
        if settlement is not None and settlement.transitioned:
            settlements.append(settlement)
    return settlements


# ---------------------------------------------------------------------------
# 结果公告
# ---------------------------------------------------------------------------


async def unannounced_round_ids() -> list[int]:
    """已终结但结果还没写到面板上的轮次（进程在结算提交之后、编辑面板之前退出的情形）。"""
    async with sql.connect() as connection:
        return await gamble_repository.unannounced_round_ids(connection)


async def mark_announced(round_id: int) -> None:
    async with sql.transaction() as connection:
        await gamble_repository.mark_announced(connection, round_id)
