"""每日抽奖（/lottery）的业务操作。不依赖 Telegram。

资格判断、奖励入账、时间戳写入在同一个事务里，要么全部生效要么都不生效；op_key 由用户与「上一次抽奖
时间」派生，同一个资格窗口里的重试、并发奖励只入账一次。
"""

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from core import balance, sql

from ..repositories import lottery as lottery_repository

# 防止同一用户并发抽奖的进程内标记；数据库层的串行靠 user 行锁。
lottery_locks: dict[int, bool] = {}

LOTTERY_COOLDOWN = timedelta(hours=24)
LOTTERY_PROBABILITIES = [0.4, 0.1, 0.5]


class LotteryStatus(StrEnum):
    WON = "won"
    NOT_REGISTERED = "not_registered"
    COOLING_DOWN = "cooling_down"  # 距离上一次抽奖不满 24 小时
    BUSY = "busy"  # 同一用户上一次抽奖还没有处理完


@dataclass(frozen=True, slots=True)
class LotteryOutcome:
    status: LotteryStatus
    coins: int = 0  # WON：本次赢得的金币


def draw_lottery_coins(rng: random.Random | None = None) -> int:
    """抽一次奖励：40% 为 1-4，10% 为 11-20，50% 为 5-10，档内均匀。"""
    rng = rng or random.Random()
    coins_distribution = [
        rng.choice(range(1, 5)),
        rng.choice(range(11, 21)),
        rng.choice(range(5, 11)),
    ]
    return rng.choices(coins_distribution, LOTTERY_PROBABILITIES)[0]


def lottery_op_key(user_id: int, last_lottery_date: datetime | None) -> str:
    """一次抽奖资格对应的 op_key：由用户与「上一次抽奖时间」决定。

    资格窗口只会随着时间戳写入而前进，所以同一个窗口里的重试、并发都得到同一个 op_key，
    奖励只能入账一次；窗口前进之后才会出现新的 op_key。从没抽过奖记为 `never`。
    """
    stamp = "never" if last_lottery_date is None else last_lottery_date.strftime("%Y%m%dT%H%M%S")
    return balance.make_op_key("lottery", user_id, stamp)


async def lottery(user_id: int) -> LotteryOutcome:
    """抽奖：资格判断、奖励入账、时间戳写入在同一个事务里，要么全部生效要么都不生效。"""
    async with sql.transaction() as connection:
        try:
            # 先锁用户行，同一用户的抽奖、签到等余额变动都在这里串行。
            await balance.lock_user(connection, user_id)
        except balance.UserNotFound:
            return LotteryOutcome(LotteryStatus.NOT_REGISTERED)

        # 用户行已经锁住，这个事务的第一次一致性读发生在拿到锁之后，能看到上一个持锁者
        # 提交的时间戳。不对 user_lottery 做 FOR UPDATE：行可能还不存在，间隙锁会让
        # 两个首次抽奖的用户互相死锁。
        last_lottery_date = await lottery_repository.get_last_lottery_date(
            user_id, connection=connection
        )
        if last_lottery_date and datetime.now() - last_lottery_date < LOTTERY_COOLDOWN:
            return LotteryOutcome(LotteryStatus.COOLING_DOWN)

        coins = draw_lottery_coins()
        await balance.credit(
            connection,
            user_id,
            coins,
            op_key=lottery_op_key(user_id, last_lottery_date),
            reason="lottery",
        )
        await lottery_repository.save_last_lottery_date(connection, user_id, datetime.now())
    return LotteryOutcome(LotteryStatus.WON, coins)


async def async_lottery(user_id: int) -> LotteryOutcome:
    """适配层调用的入口：同一用户上一次抽奖还在处理时直接返回 BUSY。"""
    if user_id in lottery_locks:
        return LotteryOutcome(LotteryStatus.BUSY)

    try:
        lottery_locks[user_id] = True
        return await lottery(user_id)
    finally:
        lottery_locks.pop(user_id, None)
