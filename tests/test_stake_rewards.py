from datetime import datetime, timedelta
from decimal import Decimal

from features.economy import stake_coin


def _stake(amount, stake_time, last_reward_time=None):
    return {
        "stake_amount": amount,
        "stake_time": stake_time,
        "last_reward_time": last_reward_time,
    }


def test_seven_day_reward_is_rounded_after_interval_accumulates():
    now = datetime(2026, 7, 8, 12, 0, 0)
    stake_time = now - timedelta(days=7)

    reward, intervals, last_reward_time = stake_coin._calculate_reward_window(
        _stake(100, stake_time),
        0.3,
        now=now,
    )

    assert intervals == 1
    assert reward == 2
    assert last_reward_time == stake_time


def test_reward_is_not_available_before_full_seven_days():
    now = datetime(2026, 7, 8, 12, 0, 0)
    stake_time = now - timedelta(days=7) + timedelta(seconds=1)

    reward, intervals, _ = stake_coin._calculate_reward_window(
        _stake(100, stake_time),
        0.3,
        now=now,
    )

    assert intervals == 0
    assert reward == 0


def test_fractional_reward_carries_across_intervals_before_rounding():
    now = datetime(2026, 7, 15, 12, 0, 0)
    stake_time = now - timedelta(days=14)

    reward, intervals, _ = stake_coin._calculate_reward_window(
        _stake(10, stake_time),
        1.0,
        now=now,
    )

    assert intervals == 2
    assert reward == 1


def test_payable_intervals_respect_pool_balance():
    intervals = stake_coin._calculate_payable_intervals(
        100,
        1.0,
        intervals_passed=3,
        pool_balance=Decimal("14"),
    )

    assert intervals == 2
