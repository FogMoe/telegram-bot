"""签到与抽奖：资格判断、状态写入、奖励入账在同一个事务里（真实 MySQL）。"""

from datetime import date, datetime, timedelta

import pytest
from economy_support import gather_all, ledger_rows, seed_user, user_state
from mysql_support import execute, fetch, fetch_scalar, run

from core import balance
from features.economy import coins as coins_handlers
from features.economy.operations import checkin, lottery
from features.economy.operations.checkin import CheckinStatus
from features.economy.operations.lottery import LotteryStatus
from features.economy.repositories import lottery as lottery_repository

TODAY = date(2026, 10, 5)


def checkin_row(url, user_id=1):
    rows = fetch(
        url,
        "SELECT last_checkin_date, consecutive_days FROM user_checkin WHERE user_id = %s",
        (user_id,),
    )
    return rows[0] if rows else None


def lottery_row(url, user_id=1):
    rows = fetch(url, "SELECT last_lottery_date FROM user_lottery WHERE user_id = %s", (user_id,))
    return rows[0] if rows else None


class TestCheckin:
    def test_first_checkin_records_the_day_and_credits_the_reward_once(self, app_database):
        seed_user(app_database, 1)

        result = run(checkin.process_checkin(1, today=TODAY))

        assert result.status is CheckinStatus.CHECKED_IN
        assert (result.consecutive_days, result.reward) == (1, 1)
        assert checkin_row(app_database) == {"last_checkin_date": TODAY, "consecutive_days": 1}
        assert user_state(app_database, 1)["free"] == 1
        assert [row["op_key"] for row in ledger_rows(app_database)] == ["checkin:1:2026-10-05"]

    def test_a_second_checkin_on_the_same_day_is_refused_without_another_credit(
        self, app_database
    ):
        seed_user(app_database, 1)
        run(checkin.process_checkin(1, today=TODAY))

        again = run(checkin.process_checkin(1, today=TODAY))

        assert again.status is CheckinStatus.ALREADY_CHECKED_IN
        assert again.consecutive_days == 1
        assert user_state(app_database, 1)["free"] == 1
        assert len(ledger_rows(app_database)) == 1

    def test_consecutive_days_and_reward_tiers_are_preserved(self, app_database):
        seed_user(app_database, 1)
        execute(
            app_database,
            (
                "INSERT INTO user_checkin (user_id, last_checkin_date, consecutive_days) "
                "VALUES (1, %s, 5)",
                (TODAY - timedelta(days=1),),
            ),
        )

        result = run(checkin.process_checkin(1, today=TODAY))

        assert (result.consecutive_days, result.reward) == (6, 2)
        assert user_state(app_database, 1)["free"] == 2

    def test_a_missed_day_resets_the_streak(self, app_database):
        seed_user(app_database, 1)
        execute(
            app_database,
            (
                "INSERT INTO user_checkin (user_id, last_checkin_date, consecutive_days) "
                "VALUES (1, %s, 9)",
                (TODAY - timedelta(days=2),),
            ),
        )

        result = run(checkin.process_checkin(1, today=TODAY))

        assert (result.consecutive_days, result.reward) == (1, 1)

    def test_a_credit_failure_leaves_no_checkin_date_and_the_retry_succeeds_once(
        self, app_database, monkeypatch
    ):
        seed_user(app_database, 1)
        real_credit = balance.credit

        async def failing_credit(*args, **kwargs):
            raise RuntimeError("入账失败")

        monkeypatch.setattr(balance, "credit", failing_credit)
        with pytest.raises(RuntimeError):
            run(checkin.process_checkin(1, today=TODAY))

        # 日期没有落库：重试不会被判成「已经签到过」。
        assert checkin_row(app_database) is None
        assert user_state(app_database, 1)["free"] == 0

        monkeypatch.setattr(balance, "credit", real_credit)
        retry = run(checkin.process_checkin(1, today=TODAY))

        assert retry.status is CheckinStatus.CHECKED_IN
        assert user_state(app_database, 1)["free"] == 1
        assert len(ledger_rows(app_database)) == 1

    def test_concurrent_checkins_succeed_once(self, app_database):
        seed_user(app_database, 1)

        async def scenario():
            return await gather_all(
                *[checkin.process_checkin(1, today=TODAY) for _ in range(5)]
            )

        results = run(scenario())

        assert all(not isinstance(item, Exception) for item in results), results
        assert sorted(item.status is CheckinStatus.CHECKED_IN for item in results) == [False] * 4 + [True]
        assert user_state(app_database, 1)["free"] == 1
        assert len(ledger_rows(app_database)) == 1

    def test_unregistered_users_are_reported(self, app_database):
        with pytest.raises(balance.UserNotFound):
            run(checkin.process_checkin(404, today=TODAY))

        assert checkin_row(app_database, 404) is None


class TestLottery:
    def test_a_draw_credits_once_and_records_the_timestamp(self, app_database, monkeypatch):
        seed_user(app_database, 1)
        monkeypatch.setattr(lottery, "draw_lottery_coins", lambda rng=None: 7)

        outcome = run(lottery.lottery(1))

        assert (outcome.status, outcome.coins) == (LotteryStatus.WON, 7)
        assert "7" in coins_handlers.lottery_message(outcome)
        assert user_state(app_database, 1)["free"] == 7
        assert lottery_row(app_database)["last_lottery_date"] is not None
        rows = ledger_rows(app_database)
        assert [(row["op_key"], row["delta_free"], row["reason"]) for row in rows] == [
            ("lottery:1:never", 7, "lottery")
        ]

    def test_a_second_draw_within_24_hours_is_refused(self, app_database, monkeypatch):
        seed_user(app_database, 1)
        monkeypatch.setattr(lottery, "draw_lottery_coins", lambda rng=None: 3)
        run(lottery.lottery(1))

        outcome = run(lottery.lottery(1))

        assert outcome.status is LotteryStatus.COOLING_DOWN
        assert "24" in coins_handlers.lottery_message(outcome)
        assert user_state(app_database, 1)["free"] == 3
        assert len(ledger_rows(app_database)) == 1

    def test_after_24_hours_a_new_window_credits_again_under_a_new_op_key(
        self, app_database, monkeypatch
    ):
        seed_user(app_database, 1)
        monkeypatch.setattr(lottery, "draw_lottery_coins", lambda rng=None: 3)
        run(lottery.lottery(1))
        long_ago = datetime.now() - timedelta(hours=25)
        execute(
            app_database,
            ("UPDATE user_lottery SET last_lottery_date = %s WHERE user_id = 1", (long_ago,)),
        )

        run(lottery.lottery(1))

        assert user_state(app_database, 1)["free"] == 6
        keys = [row["op_key"] for row in ledger_rows(app_database)]
        assert len(keys) == 2 and len(set(keys)) == 2
        assert keys[1].startswith("lottery:1:2")

    def test_a_timestamp_write_failure_rolls_the_reward_back_and_the_retry_pays_once(
        self, app_database, monkeypatch
    ):
        seed_user(app_database, 1)
        monkeypatch.setattr(lottery, "draw_lottery_coins", lambda rng=None: 5)
        real_update = lottery_repository.save_last_lottery_date

        async def failing_update(*args, **kwargs):
            raise RuntimeError("时间戳写入失败")

        monkeypatch.setattr(lottery_repository, "save_last_lottery_date", failing_update)
        with pytest.raises(RuntimeError):
            run(lottery.lottery(1))

        assert user_state(app_database, 1)["free"] == 0
        assert ledger_rows(app_database) == []
        assert lottery_row(app_database) is None

        monkeypatch.setattr(lottery_repository, "save_last_lottery_date", real_update)
        run(lottery.lottery(1))

        assert user_state(app_database, 1)["free"] == 5
        assert len(ledger_rows(app_database)) == 1

        # 再试一次：窗口已经前进，不会第三次入账。
        run(lottery.lottery(1))
        assert user_state(app_database, 1)["free"] == 5

    def test_concurrent_draws_succeed_once(self, app_database, monkeypatch):
        seed_user(app_database, 1)
        monkeypatch.setattr(lottery, "draw_lottery_coins", lambda rng=None: 4)

        async def scenario():
            # 直接调用 lottery：进程内的 lottery_locks 只挡得住同一进程，这里验证数据库层的串行。
            return await gather_all(*[lottery.lottery(1) for _ in range(5)])

        results = run(scenario())

        assert all(not isinstance(item, Exception) for item in results), results
        winners = [item for item in results if item.status is LotteryStatus.WON]
        assert len(winners) == 1
        assert user_state(app_database, 1)["free"] == 4
        assert len(ledger_rows(app_database)) == 1
        assert fetch_scalar(app_database, "SELECT COUNT(*) FROM user_lottery WHERE user_id = 1") == 1

    def test_first_time_draws_by_different_users_do_not_deadlock(self, app_database, monkeypatch):
        for user_id in range(1, 9):
            seed_user(app_database, user_id)
        monkeypatch.setattr(lottery, "draw_lottery_coins", lambda rng=None: 2)

        async def scenario():
            return await gather_all(*[lottery.lottery(user_id) for user_id in range(1, 9)])

        results = run(scenario())

        assert all(not isinstance(item, Exception) for item in results), results
        assert fetch_scalar(app_database, "SELECT SUM(coins) FROM `user`") == 16

    def test_unregistered_users_are_asked_to_register(self, app_database):
        outcome = run(lottery.lottery(404))

        assert outcome.status is LotteryStatus.NOT_REGISTERED
        assert "/me" in coins_handlers.lottery_message(outcome)
        assert ledger_rows(app_database) == []
        assert lottery_row(app_database, 404) is None
