"""每日签到的业务操作：连续天数、奖励档位，以及资格判断、签到日期与奖励入账的同一个事务。不依赖 Telegram。"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum

from core import balance, sql

from ..repositories import checkin as checkin_repository

# 连续签到达到这个天数后奖励不再增加；展示用的进度条也按它计算。
MAX_REWARD_DAYS = 31


def calculate_checkin_reward(consecutive_days: int) -> int:
    if consecutive_days <= 5:
        return 1
    if consecutive_days <= 10:
        return 2
    if consecutive_days <= 15:
        return 3
    if consecutive_days <= 20:
        return 4
    if consecutive_days <= 25:
        return 5
    if consecutive_days <= 30:
        return 6
    return 7


def checkin_op_key(user_id: int, day: date) -> str:
    """每个用户每天只有一个签到身份，奖励入账以它为幂等键。"""
    return balance.make_op_key("checkin", user_id, day.isoformat())


class CheckinStatus(StrEnum):
    CHECKED_IN = "checked_in"
    ALREADY_CHECKED_IN = "already_checked_in"  # 今天已经签到过，没有入账


@dataclass(frozen=True, slots=True)
class CheckinOutcome:
    status: CheckinStatus
    consecutive_days: int
    reward: int = 0  # CHECKED_IN：本次奖励的金币


async def process_checkin(user_id: int, *, today: date | None = None) -> CheckinOutcome:
    """签到：资格判断、签到日期写入、奖励入账在同一个事务里。

    入账失败（异常）时整个事务回滚，日期不会落库，重试仍然可以签到。用户不存在抛
    `balance.UserNotFound`。
    """
    today = today or datetime.now().date()
    async with sql.transaction() as connection:
        # 先锁用户行，同一用户的并发签到在这里串行；之后第一次一致性读能看到上一个持锁者提交的日期。
        await balance.lock_user(connection, user_id)
        record = await checkin_repository.get_checkin(user_id, connection=connection)

        if record and record.last_checkin_date == today:
            return CheckinOutcome(CheckinStatus.ALREADY_CHECKED_IN, record.consecutive_days)

        consecutive_days = 1
        if record and record.last_checkin_date == today - timedelta(days=1):
            consecutive_days = record.consecutive_days + 1

        reward_coins = calculate_checkin_reward(consecutive_days)
        await checkin_repository.save_checkin(connection, user_id, today, consecutive_days)
        await balance.credit(
            connection,
            user_id,
            reward_coins,
            op_key=checkin_op_key(user_id, today),
            reason="checkin",
        )

    return CheckinOutcome(CheckinStatus.CHECKED_IN, consecutive_days, reward_coins)
