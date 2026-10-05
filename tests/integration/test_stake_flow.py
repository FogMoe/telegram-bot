"""质押：扣款、领奖、赎回与奖池同事务；锁顺序固定为 user 行在前、奖池行在后（真实 MySQL）。"""

from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from economy_support import (
    gather_all,
    ledger_keys,
    ledger_rows,
    make_callback_update,
    make_command_update,
    pool_balance,
    pool_rows,
    seed_user,
    user_state,
)
from mysql_support import execute, fetch, run

from fogmoe_telegram_bot.core import balance, sql, stake_reward_pool
from fogmoe_telegram_bot.features.economy import stake_coin
from fogmoe_telegram_bot.features.economy.operations import stake as stake_operations
from fogmoe_telegram_bot.features.economy.operations.stake import (
    CollectStatus,
    OpenStatus,
    WithdrawStatus,
)

NOW = datetime.now().replace(microsecond=0)
EIGHT_DAYS_AGO = NOW - timedelta(days=8)


def set_pool(url, amount):
    execute(url, ("UPDATE stake_reward_pool SET balance = %s WHERE id = 1", (amount,)))


def seed_stake(url, user_id, amount, *, stake_time=EIGHT_DAYS_AGO, last_reward_time=None):
    execute(
        url,
        (
            "INSERT INTO user_stakes (user_id, stake_amount, stake_time, last_reward_time) "
            "VALUES (%s, %s, %s, %s)",
            (user_id, amount, stake_time, last_reward_time),
        ),
    )


def stake_row(url, user_id=1):
    rows = fetch(
        url,
        "SELECT stake_amount, stake_time, last_reward_time FROM user_stakes WHERE user_id = %s",
        (user_id,),
    )
    return rows[0] if rows else None


def coins(url, user_id=1):
    state = user_state(url, user_id)
    return state["free"] + state["paid"]


def fail_after(monkeypatch, module, name):
    real = getattr(module, name)

    async def wrapper(*args, **kwargs):
        await real(*args, **kwargs)
        raise RuntimeError("injected failure")

    monkeypatch.setattr(module, name, wrapper)


class TestOpenStake:
    def test_stake_debits_and_records_the_stake_together(self, app_database):
        seed_user(app_database, 1, free=300)

        outcome = run(stake_operations.open_stake(1, 100, op_key="stake:7:11"))

        assert outcome.status is OpenStatus.STAKED
        assert coins(app_database) == 200
        assert stake_row(app_database)["stake_amount"] == 100
        assert [(row["op_key"], row["kind"], row["reason"]) for row in ledger_rows(app_database)] == [
            ("stake:7:11", "debit", "stake")
        ]

    def test_the_same_command_delivered_twice_is_charged_once(self, app_database):
        seed_user(app_database, 1, free=300)

        run(stake_operations.open_stake(1, 100, op_key="stake:7:11"))
        replay = run(stake_operations.open_stake(1, 100, op_key="stake:7:11"))

        assert replay.status is OpenStatus.REPLAYED
        assert coins(app_database) == 200
        assert len(ledger_rows(app_database)) == 1

    def test_an_insufficient_balance_leaves_no_stake_and_no_ledger_row(self, app_database):
        seed_user(app_database, 1, free=50)

        outcome = run(stake_operations.open_stake(1, 100, op_key="stake:7:11"))

        assert (outcome.status, outcome.balance_total) == (OpenStatus.INSUFFICIENT, 50)
        assert coins(app_database) == 50
        assert stake_row(app_database) is None
        assert ledger_rows(app_database) == []

    def test_a_second_stake_is_refused_before_any_charge(self, app_database):
        seed_user(app_database, 1, free=300)
        seed_stake(app_database, 1, 100, stake_time=NOW)

        outcome = run(stake_operations.open_stake(1, 50, op_key="stake:7:12"))

        assert outcome.status is OpenStatus.ALREADY_STAKED
        assert coins(app_database) == 300
        assert ledger_rows(app_database) == []

    def test_an_unregistered_user_is_reported(self, app_database):
        outcome = run(stake_operations.open_stake(404, 10, op_key="stake:7:11"))

        assert outcome.status is OpenStatus.NOT_REGISTERED

    def test_concurrent_stakes_by_one_user_open_exactly_one_stake(self, app_database):
        seed_user(app_database, 1, free=1000)

        async def scenario():
            return await gather_all(
                *[
                    stake_operations.open_stake(1, 100, op_key=f"stake:7:{index}")
                    for index in range(5)
                ]
            )

        results = run(scenario())

        assert all(not isinstance(item, Exception) for item in results), results
        statuses = sorted(item.status for item in results)
        assert statuses == [OpenStatus.ALREADY_STAKED] * 4 + [OpenStatus.STAKED]
        assert coins(app_database) == 900
        assert len(ledger_rows(app_database)) == 1

    def test_first_stakes_of_different_users_do_not_block_each_other(self, app_database):
        for user_id in range(1, 9):
            seed_user(app_database, user_id, free=500)

        async def scenario():
            return await gather_all(
                *[
                    stake_operations.open_stake(user_id, 100, op_key=f"stake:7:{user_id}")
                    for user_id in range(1, 9)
                ]
            )

        results = run(scenario())

        assert all(not isinstance(item, Exception) for item in results), results
        assert {item.status for item in results} == {OpenStatus.STAKED}
        assert all(coins(app_database, user_id) == 400 for user_id in range(1, 9))

    def test_a_failure_after_the_debit_leaves_no_stake_and_no_charge(
        self, app_database, monkeypatch
    ):
        seed_user(app_database, 1, free=300)
        fail_after(monkeypatch, balance, "debit")

        with pytest.raises(RuntimeError):
            run(stake_operations.open_stake(1, 100, op_key="stake:7:11"))

        assert coins(app_database) == 300
        assert stake_row(app_database) is None
        assert ledger_rows(app_database) == []

    def test_the_command_handler_confirms_a_stake(self, app_database):
        seed_user(app_database, 1, free=300)
        update = make_command_update(user_id=1, chat_id=5, message_id=42)
        context = SimpleNamespace()

        run(stake_coin.stake_coins(update, context, 100))

        assert "成功质押 100 金币" in update.message.reply_text.texts[0]
        assert ledger_keys(app_database) == ["stake:5:42"]

    def test_the_command_handler_reports_the_balance_when_it_is_too_low(self, app_database):
        seed_user(app_database, 1, free=30)
        update = make_command_update(user_id=1, chat_id=5, message_id=42)

        run(stake_coin.stake_coins(update, SimpleNamespace(), 100))

        assert "当前余额: 30 金币" in update.message.reply_text.texts[0]
        assert ledger_rows(app_database) == []


class TestCollectReward:
    def test_collect_credits_the_user_debits_the_pool_and_moves_the_window(self, app_database):
        seed_user(app_database, 1, free=0)
        seed_stake(app_database, 1, 1000)
        set_pool(app_database, 100)

        outcome = run(stake_operations.collect_stake_reward(1))

        assert (outcome.status, outcome.reward) == (CollectStatus.COLLECTED, 21)
        assert coins(app_database) == 21
        assert pool_balance(app_database) == Decimal("79")
        assert stake_row(app_database)["last_reward_time"] == EIGHT_DAYS_AGO + timedelta(days=7)
        key = stake_operations.stake_collect_op_key(1, EIGHT_DAYS_AGO, EIGHT_DAYS_AGO)
        assert ledger_keys(app_database) == [key]
        assert [(row["op_key"], row["kind"], row["ref"]) for row in pool_rows(app_database)] == [
            (key, "debit", key)
        ]
        assert run(balance.audit_ledger()).clean

    def test_collecting_again_in_the_same_window_pays_nothing(self, app_database):
        seed_user(app_database, 1, free=0)
        seed_stake(app_database, 1, 1000)
        set_pool(app_database, 100)
        run(stake_operations.collect_stake_reward(1))

        again = run(stake_operations.collect_stake_reward(1))

        assert again.status is CollectStatus.NOT_YET
        assert coins(app_database) == 21
        assert pool_balance(app_database) == Decimal("79")
        assert len(ledger_rows(app_database)) == 1

    def test_no_stake_and_too_early_are_reported(self, app_database):
        seed_user(app_database, 1)
        seed_user(app_database, 2)
        seed_stake(app_database, 2, 1000, stake_time=NOW - timedelta(days=3))

        assert run(stake_operations.collect_stake_reward(1)).status is CollectStatus.NO_STAKE
        assert run(stake_operations.collect_stake_reward(2)).status is CollectStatus.NOT_YET
        assert ledger_rows(app_database) == []

    def test_an_empty_pool_pays_nothing_and_changes_nothing(self, app_database):
        seed_user(app_database, 1, free=0)
        seed_stake(app_database, 1, 1000)
        set_pool(app_database, 10)

        outcome = run(stake_operations.collect_stake_reward(1))

        assert outcome.status is CollectStatus.POOL_EMPTY
        assert coins(app_database) == 0
        assert pool_balance(app_database) == Decimal("10")
        assert stake_row(app_database)["last_reward_time"] is None
        assert ledger_rows(app_database) == []

    def test_a_pool_that_covers_only_some_intervals_pays_those(self, app_database):
        seed_user(app_database, 1, free=0)
        seed_stake(app_database, 1, 1000, stake_time=NOW - timedelta(days=15))
        set_pool(app_database, 30)

        outcome = run(stake_operations.collect_stake_reward(1))

        assert (outcome.status, outcome.reward) == (CollectStatus.COLLECTED, 21)
        assert pool_balance(app_database) == Decimal("9")
        assert stake_row(app_database)["last_reward_time"] == NOW - timedelta(days=15) + timedelta(
            days=7
        )

    def test_concurrent_collects_by_one_user_pay_once(self, app_database):
        seed_user(app_database, 1, free=0)
        seed_stake(app_database, 1, 1000)
        set_pool(app_database, 100)

        async def scenario():
            return await gather_all(*[stake_operations.collect_stake_reward(1) for _ in range(6)])

        results = run(scenario())

        assert all(not isinstance(item, Exception) for item in results), results
        statuses = [item.status for item in results]
        assert statuses.count(CollectStatus.COLLECTED) == 1
        assert statuses.count(CollectStatus.NOT_YET) == 5
        assert coins(app_database) == 21
        assert pool_balance(app_database) == Decimal("79")

    def test_concurrent_collect_and_withdraw_never_pay_the_reward_twice(self, app_database):
        seed_user(app_database, 1, free=0)
        seed_stake(app_database, 1, 1000)
        set_pool(app_database, 100)

        async def scenario():
            return await gather_all(
                stake_operations.collect_stake_reward(1),
                stake_operations.withdraw_stake_principal(1),
                stake_operations.collect_stake_reward(1),
                stake_operations.withdraw_stake_principal(1),
            )

        results = run(scenario())

        assert all(not isinstance(item, Exception) for item in results), results
        # 无论谁先拿到锁：回报只发一次（21），本金只退一次（1000 - 30 手续费）。
        assert coins(app_database) == 21 + 970
        assert pool_balance(app_database) == Decimal("79")
        assert stake_row(app_database) is None
        assert run(balance.audit_ledger()).clean

    def test_the_pool_is_never_overpaid_when_stakers_compete_for_it(self, app_database):
        for user_id in range(1, 7):
            seed_user(app_database, user_id, free=0)
            seed_stake(app_database, user_id, 1000)
        set_pool(app_database, 30)

        async def scenario():
            return await gather_all(
                *[stake_operations.collect_stake_reward(user_id) for user_id in range(1, 7)]
            )

        results = run(scenario())

        assert all(not isinstance(item, Exception) for item in results), results
        paid = sum(item.reward for item in results)
        assert 0 < paid <= 30
        assert pool_balance(app_database) == Decimal(30 - paid)
        assert sum(coins(app_database, user_id) for user_id in range(1, 7)) == paid
        assert run(balance.audit_ledger()).clean

    def test_a_pool_failure_rolls_back_the_credit_and_the_window(
        self, app_database, monkeypatch
    ):
        seed_user(app_database, 1, free=0)
        seed_stake(app_database, 1, 1000)
        set_pool(app_database, 100)
        fail_after(monkeypatch, stake_reward_pool, "debit_pool")

        with pytest.raises(RuntimeError):
            run(stake_operations.collect_stake_reward(1))

        assert coins(app_database) == 0
        assert pool_balance(app_database) == Decimal("100")
        assert stake_row(app_database)["last_reward_time"] is None
        assert ledger_rows(app_database) == []
        assert pool_rows(app_database) == []

    def test_the_callback_confirms_the_reward(self, app_database):
        seed_user(app_database, 1, free=0)
        seed_stake(app_database, 1, 1000)
        set_pool(app_database, 100)
        update, answer, edit = make_callback_update(from_user_id=1, data="stake_collect_1")

        run(stake_coin.stake_callback(update, SimpleNamespace()))

        assert answer.texts == ["成功领取 21 金币回报！"]
        assert "您已成功领取 21 金币的回报" in edit.texts[0]

    def test_the_callback_refuses_someone_elses_button(self, app_database):
        seed_user(app_database, 1)
        seed_user(app_database, 2)
        seed_stake(app_database, 1, 1000)
        set_pool(app_database, 100)
        update, answer, _ = make_callback_update(from_user_id=2, data="stake_collect_1")

        run(stake_coin.stake_callback(update, SimpleNamespace()))

        assert answer.texts == ["这不是你的质押，你不能操作。"]
        assert ledger_rows(app_database) == []


class TestWithdraw:
    def test_withdraw_returns_the_principal_minus_the_fee_and_clears_the_stake(
        self, app_database
    ):
        seed_user(app_database, 1, free=0)
        seed_stake(app_database, 1, 1000, stake_time=NOW - timedelta(days=3))

        outcome = run(stake_operations.withdraw_stake_principal(1))

        assert (outcome.status, outcome.principal, outcome.fee, outcome.reward) == (
            WithdrawStatus.WITHDRAWN,
            970,
            30,
            0,
        )
        assert coins(app_database) == 970
        assert stake_row(app_database) is None
        assert [row["reason"] for row in ledger_rows(app_database)] == ["stake_withdraw"]
        assert "未满7天" in stake_coin.withdraw_message(outcome)

    def test_a_due_reward_is_paid_from_the_pool_in_the_same_step(self, app_database):
        seed_user(app_database, 1, free=0)
        seed_stake(app_database, 1, 1000)
        set_pool(app_database, 100)

        outcome = run(stake_operations.withdraw_stake_principal(1))

        assert outcome.reward == 21
        assert coins(app_database) == 970 + 21
        assert pool_balance(app_database) == Decimal("79")
        assert len(ledger_rows(app_database)) == 2
        assert [row["reason"] for row in pool_rows(app_database)] == ["stake_reward"]
        assert "并获得回报 21 金币" in stake_coin.withdraw_message(outcome)

    def test_an_empty_pool_still_returns_the_principal(self, app_database):
        seed_user(app_database, 1, free=0)
        seed_stake(app_database, 1, 1000)
        set_pool(app_database, 5)

        outcome = run(stake_operations.withdraw_stake_principal(1))

        assert outcome.reward == 0
        assert coins(app_database) == 970
        assert pool_balance(app_database) == Decimal("5")
        assert "奖励池余额不足" in stake_coin.withdraw_message(outcome)

    def test_concurrent_withdrawals_return_the_principal_once(self, app_database):
        seed_user(app_database, 1, free=0)
        seed_stake(app_database, 1, 1000, stake_time=NOW - timedelta(days=1))

        async def scenario():
            return await gather_all(*[stake_operations.withdraw_stake_principal(1) for _ in range(5)])

        results = run(scenario())

        assert all(not isinstance(item, Exception) for item in results), results
        assert sorted(item.status for item in results) == [WithdrawStatus.NO_STAKE] * 4 + [
            WithdrawStatus.WITHDRAWN
        ]
        assert coins(app_database) == 970

    def test_a_failure_after_the_credit_keeps_the_stake_and_the_balance(
        self, app_database, monkeypatch
    ):
        seed_user(app_database, 1, free=0)
        seed_stake(app_database, 1, 1000)
        set_pool(app_database, 100)
        fail_after(monkeypatch, balance, "credit")

        with pytest.raises(RuntimeError):
            run(stake_operations.withdraw_stake_principal(1))

        assert coins(app_database) == 0
        assert stake_row(app_database) is not None
        assert pool_balance(app_database) == Decimal("100")
        assert ledger_rows(app_database) == []

    def test_the_callback_reports_what_was_returned(self, app_database):
        seed_user(app_database, 1, free=0)
        seed_stake(app_database, 1, 1000, stake_time=NOW - timedelta(days=1))
        update, answer, edit = make_callback_update(from_user_id=1, data="stake_withdraw_1")

        run(stake_coin.stake_callback(update, SimpleNamespace()))

        assert "您已取出质押本金 970 金币（手续费 30 金币）" in answer.texts[0]
        assert "您目前没有质押金币" in edit.texts[0]


class TestLockOrderUnderContention:
    def test_stake_rewards_and_pool_contributions_do_not_deadlock(
        self, app_database, monkeypatch
    ):
        """领奖/赎回（user 行 -> 奖池行）与对话计费式的贡献（扣费 user 行 -> 奖池行）同时进行。

        除了结果正确，还要求没有发生过死锁：固定的加锁顺序让死锁根本不会出现，而不是靠重试兜底。
        """
        deadlocks = []
        real_is_deadlock = sql.is_deadlock_error

        def recording_is_deadlock(exc):
            found = real_is_deadlock(exc)
            if found:
                deadlocks.append(exc)
            return found

        monkeypatch.setattr(sql, "is_deadlock_error", recording_is_deadlock)
        users = range(1, 13)
        for user_id in users:
            seed_user(app_database, user_id, free=50)
            if user_id <= 8:
                seed_stake(app_database, user_id, 500)
        set_pool(app_database, 1000)

        async def spend_and_contribute(user_id):
            op_key = f"chat:{user_id}:1"

            async def work(connection):
                await balance.debit(connection, user_id, 5, op_key=op_key, reason="ai_chat")
                await stake_reward_pool.credit_share_of_spend(
                    connection, 5, spend_op_key=op_key
                )

            await balance.run_in_transaction(work)

        async def scenario():
            jobs = []
            for user_id in users:
                jobs.append(spend_and_contribute(user_id))
                if user_id <= 4:
                    jobs.append(stake_operations.collect_stake_reward(user_id))
                elif user_id <= 8:
                    jobs.append(stake_operations.withdraw_stake_principal(user_id))
            jobs.append(
                stake_reward_pool.debit_pool_standalone(
                    Decimal("1.00"), op_key="manual:1", reason="manual"
                )
            )
            return await gather_all(*jobs)

        results = run(scenario())

        assert all(not isinstance(item, Exception) for item in results), results
        assert deadlocks == []
        assert pool_balance(app_database) >= 0
        assert run(balance.audit_ledger()).clean
        assert stake_row(app_database, 5) is None  # 5-8 号已赎回
