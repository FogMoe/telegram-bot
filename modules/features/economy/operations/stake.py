"""质押的业务操作：质押、领取回报、赎回本金，以及回报率与回报的计算规则。不依赖 Telegram。

质押操作：余额、奖池与质押记录在同一个事务里提交。

加锁顺序固定为「先 user 行、后奖池行」（与对话计费等其他路径一致）：每个操作先 `lock_user`，
再读自己的质押记录，需要奖池时才锁奖池行。同一用户的质押、领奖、赎回因此串行；不同用户只在奖池行上
排队。死锁时 `run_in_transaction` 整个事务重跑，所以操作函数里不做事务外的副作用，消息在事务提交之后
由调用方发送。op_key 见 docs/balance-service.md。
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncConnection

from core import balance, stake_reward_pool

from ..repositories import stake as stake_repository
from ..repositories.stake import StakeRecord

REWARD_INTERVAL_DAYS = 7
WITHDRAW_FEE_RATE = 0.03
MAX_DAILY_RATE = 0.3
MIN_DAILY_RATE = 0.05


async def calculate_reward_rate(*, connection: AsyncConnection | None = None) -> float:
    total_coins = await stake_repository.sum_user_coins(connection=connection)
    total_staked = await stake_repository.sum_staked(connection=connection)

    if total_staked == 0 or total_coins == 0:
        return MAX_DAILY_RATE

    stake_ratio = min(1.0, float(total_staked) / (float(total_coins) + float(total_staked)))
    max_rate = MAX_DAILY_RATE
    min_rate = MIN_DAILY_RATE
    reward_rate = max_rate - stake_ratio * (max_rate - min_rate)

    return reward_rate


def _calculate_reward_for_intervals(
    stake_amount: int | Decimal,
    reward_rate: float,
    intervals: int,
) -> int:
    if intervals <= 0:
        return 0

    reward_days = intervals * REWARD_INTERVAL_DAYS
    reward = (
        Decimal(str(stake_amount))
        * Decimal(str(reward_rate))
        * Decimal(reward_days)
        / Decimal("100")
    )
    return max(0, int(reward))


def _calculate_payable_intervals(
    stake_amount: int | Decimal,
    reward_rate: float,
    intervals_passed: int,
    pool_balance: Decimal | int | None,
) -> int:
    pool = Decimal(str(pool_balance or 0))
    if intervals_passed <= 0 or pool <= 0:
        return 0

    low = 0
    high = intervals_passed
    while low < high:
        mid = (low + high + 1) // 2
        reward = _calculate_reward_for_intervals(stake_amount, reward_rate, mid)
        if Decimal(reward) <= pool:
            low = mid
        else:
            high = mid - 1

    if _calculate_reward_for_intervals(stake_amount, reward_rate, low) <= 0:
        return 0
    return low


def _calculate_reward_window(
    user_stake: StakeRecord,
    reward_rate: float,
    *,
    now: datetime | None = None,
) -> tuple[int, int, datetime]:
    last_reward_time = user_stake.last_reward_time or user_stake.stake_time
    now = now or datetime.now()
    elapsed_seconds = max(0, (now - last_reward_time).total_seconds())
    days_passed = int(elapsed_seconds // 86400)
    intervals_passed = days_passed // REWARD_INTERVAL_DAYS
    reward = _calculate_reward_for_intervals(
        user_stake.stake_amount,
        reward_rate,
        intervals_passed,
    )
    return reward, intervals_passed, last_reward_time


async def get_user_stake(user_id: int, *, connection: AsyncConnection | None = None) -> StakeRecord | None:
    return await stake_repository.get_stake(user_id, connection=connection)


async def calculate_available_reward(user_id: int) -> int:
    user_stake = await get_user_stake(user_id)
    if not user_stake or user_stake.stake_amount <= 0:
        return 0

    reward_rate = await calculate_reward_rate()
    reward, _, _ = _calculate_reward_window(user_stake, reward_rate)
    return reward


_STAMP_FORMAT = "%Y%m%dT%H%M%S"


def stake_open_op_key(chat_id: int, message_id: int) -> str:
    """质押扣款：以 /stake 命令消息为身份，同一条命令被重复投递不会再扣一次。"""
    return balance.make_op_key("stake", chat_id, message_id)


def stake_collect_op_key(user_id: int, stake_time: datetime, window_start: datetime) -> str:
    """领奖：一次质押（user_id + stake_time）里从 `window_start` 起的那一段领奖窗口。

    领奖成功会把 last_reward_time 推进到窗口之后，所以同一个窗口只会领一次；
    奖励入账与奖池扣减共用这个 op_key（分属两张账本）。
    """
    return balance.make_op_key(
        "stake_collect",
        user_id,
        stake_time.strftime(_STAMP_FORMAT),
        window_start.strftime(_STAMP_FORMAT),
    )


def stake_withdraw_op_key(user_id: int, stake_time: datetime) -> str:
    """赎回本金：一次质押只能赎回一次（质押记录随赎回删除）。"""
    return balance.make_op_key("stake_withdraw", user_id, stake_time.strftime(_STAMP_FORMAT))


def stake_withdraw_reward_op_key(user_id: int, stake_time: datetime) -> str:
    """赎回时顺带结算的回报；入账与奖池扣减共用。"""
    return balance.make_op_key(
        "stake_withdraw_reward", user_id, stake_time.strftime(_STAMP_FORMAT)
    )


class OpenStatus(StrEnum):
    STAKED = "staked"
    REPLAYED = "replayed"  # 同一条命令被重复投递，上一次已经成功
    ALREADY_STAKED = "already_staked"
    INSUFFICIENT = "insufficient"
    NOT_REGISTERED = "not_registered"


@dataclass(frozen=True)
class OpenStakeOutcome:
    status: OpenStatus
    balance_total: int = 0  # 余额不足时的当前余额


async def open_stake(user_id: int, amount: int, *, op_key: str) -> OpenStakeOutcome:
    """质押 `amount` 枚金币：扣款与质押记录同事务。余额不足不改动任何数据。"""

    async def work(connection: AsyncConnection) -> OpenStakeOutcome:
        try:
            balances = await balance.lock_user(connection, user_id)
        except balance.UserNotFound:
            return OpenStakeOutcome(OpenStatus.NOT_REGISTERED)

        # 同一条命令的重放：上一次的扣款与质押记录一起提交了，直接确认结果。
        if await balance.get_operation(op_key, connection=connection) is not None:
            return OpenStakeOutcome(OpenStatus.REPLAYED)
        if balances.total < amount:
            return OpenStakeOutcome(OpenStatus.INSUFFICIENT, balances.total)
        if await get_user_stake(user_id, connection=connection):
            return OpenStakeOutcome(OpenStatus.ALREADY_STAKED)

        try:
            await balance.debit(connection, user_id, amount, op_key=op_key, reason="stake")
        except balance.InsufficientBalance as exc:
            return OpenStakeOutcome(OpenStatus.INSUFFICIENT, exc.balance_total)
        await stake_repository.insert_stake(connection, user_id, amount, datetime.now())
        return OpenStakeOutcome(OpenStatus.STAKED)

    return await balance.run_in_transaction(work)


class CollectStatus(StrEnum):
    COLLECTED = "collected"
    NO_STAKE = "no_stake"
    NOT_YET = "not_yet"  # 不满一个领奖周期
    TOO_SMALL = "too_small"  # 满了周期但累计回报不足 1 金币
    POOL_EMPTY = "pool_empty"


@dataclass(frozen=True)
class CollectOutcome:
    status: CollectStatus
    reward: int = 0
    stake_amount: int = 0


async def collect_stake_reward(user_id: int) -> CollectOutcome:
    """领取回报：用户入账、奖池扣减与 last_reward_time 推进同事务。"""

    async def work(connection: AsyncConnection) -> CollectOutcome:
        try:
            await balance.lock_user(connection, user_id)
        except balance.UserNotFound:
            return CollectOutcome(CollectStatus.NO_STAKE)
        user_stake = await get_user_stake(user_id, connection=connection)
        if not user_stake:
            return CollectOutcome(CollectStatus.NO_STAKE)

        reward_rate = await calculate_reward_rate(connection=connection)
        reward_due, intervals_passed, last_reward_time = _calculate_reward_window(
            user_stake,
            reward_rate,
        )
        if intervals_passed <= 0:
            return CollectOutcome(CollectStatus.NOT_YET)
        if reward_due <= 0:
            return CollectOutcome(CollectStatus.TOO_SMALL)

        # 用户行已经锁住，现在才锁奖池行；按锁内读到的余额决定能发多少，扣减不会失败。
        pool_balance = await stake_reward_pool.get_pool_balance(
            connection=connection,
            for_update=True,
        )
        intervals_paid = _calculate_payable_intervals(
            user_stake.stake_amount,
            reward_rate,
            intervals_passed,
            pool_balance,
        )
        reward = _calculate_reward_for_intervals(
            user_stake.stake_amount,
            reward_rate,
            intervals_paid,
        )
        if intervals_paid <= 0 or reward <= 0:
            return CollectOutcome(CollectStatus.POOL_EMPTY)

        op_key = stake_collect_op_key(user_id, user_stake.stake_time, last_reward_time)
        await balance.credit(
            connection, user_id, reward, op_key=op_key, reason="stake_reward"
        )
        await stake_reward_pool.debit_pool(
            connection, reward, op_key=op_key, reason="stake_reward", ref=op_key
        )
        await stake_repository.set_last_reward_time(
            connection,
            user_id,
            last_reward_time + timedelta(days=intervals_paid * REWARD_INTERVAL_DAYS),
        )
        return CollectOutcome(
            CollectStatus.COLLECTED,
            reward=reward,
            stake_amount=user_stake.stake_amount,
        )

    return await balance.run_in_transaction(work)


class WithdrawStatus(StrEnum):
    WITHDRAWN = "withdrawn"
    NO_STAKE = "no_stake"


@dataclass(frozen=True)
class WithdrawOutcome:
    status: WithdrawStatus
    fee: int = 0
    principal: int = 0
    reward: int = 0
    reward_due: int = 0
    intervals_passed: int = 0


async def withdraw_stake_principal(user_id: int) -> WithdrawOutcome:
    """取出本金（扣 3% 手续费）并结算已满周期的回报：入账、奖池扣减、删除质押记录同事务。"""

    async def work(connection: AsyncConnection) -> WithdrawOutcome:
        try:
            await balance.lock_user(connection, user_id)
        except balance.UserNotFound:
            return WithdrawOutcome(WithdrawStatus.NO_STAKE)
        user_stake = await get_user_stake(user_id, connection=connection)
        if not user_stake:
            return WithdrawOutcome(WithdrawStatus.NO_STAKE)

        stake_amount = user_stake.stake_amount
        fee = int(stake_amount * WITHDRAW_FEE_RATE)
        refunded_principal = max(stake_amount - fee, 0)
        reward_rate = await calculate_reward_rate(connection=connection)
        reward_due, intervals_passed, _ = _calculate_reward_window(
            user_stake,
            reward_rate,
        )
        reward = 0
        if reward_due > 0 and intervals_passed > 0:
            pool_balance = await stake_reward_pool.get_pool_balance(
                connection=connection,
                for_update=True,
            )
            intervals_paid = _calculate_payable_intervals(
                user_stake.stake_amount,
                reward_rate,
                intervals_passed,
                pool_balance,
            )
            reward = _calculate_reward_for_intervals(
                user_stake.stake_amount,
                reward_rate,
                intervals_paid,
            )

        stake_time = user_stake.stake_time
        if refunded_principal > 0:
            await balance.credit(
                connection,
                user_id,
                refunded_principal,
                op_key=stake_withdraw_op_key(user_id, stake_time),
                reason="stake_withdraw",
            )
        if reward > 0:
            reward_key = stake_withdraw_reward_op_key(user_id, stake_time)
            await balance.credit(
                connection, user_id, reward, op_key=reward_key, reason="stake_reward"
            )
            await stake_reward_pool.debit_pool(
                connection, reward, op_key=reward_key, reason="stake_reward", ref=reward_key
            )

        await stake_repository.delete_stake(connection, user_id)
        return WithdrawOutcome(
            WithdrawStatus.WITHDRAWN,
            fee=fee,
            principal=refunded_principal,
            reward=reward,
            reward_due=reward_due,
            intervals_passed=intervals_passed,
        )

    return await balance.run_in_transaction(work)
