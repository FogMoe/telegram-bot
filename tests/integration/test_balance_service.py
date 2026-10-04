"""余额服务：幂等、冲突、并发、拆分、退款、事务所有权与账本对账（真实 MySQL）。"""

import pytest
from economy_support import (
    gather_all,
    ledger_keys,
    ledger_rows,
    seed_user,
    total_coins,
    user_state,
)
from mysql_support import execute, fetch, fetch_scalar, run

from core import balance, config, mysql_connection, stake_reward_pool


def credit(user_id, amount, key, **kwargs):
    return run(balance.credit_standalone(user_id, amount, op_key=key, reason="test", **kwargs))


def debit(user_id, amount, key):
    return run(balance.debit_standalone(user_id, amount, op_key=key, reason="test"))


class TestCredit:
    def test_free_credit_increases_free_balance_only(self, app_database):
        seed_user(app_database, 1, free=3, paid=2)

        result = credit(1, 5, "t:free")

        assert result.applied is True
        assert (result.delta_free, result.delta_paid) == (5, 0)
        assert (result.balance_free, result.balance_paid) == (8, 2)
        assert user_state(app_database, 1) == {"free": 8, "paid": 2, "plan": "paid"}

    def test_paid_credit_switches_the_plan_to_paid(self, app_database):
        seed_user(app_database, 1, free=1)

        result = credit(1, 50, "t:paid", kind=balance.CoinKind.PAID)

        assert (result.delta_free, result.delta_paid) == (0, 50)
        assert result.user_plan == "paid"
        assert user_state(app_database, 1) == {"free": 1, "paid": 50, "plan": "paid"}

    def test_administrator_keeps_the_admin_plan(self, app_database, monkeypatch):
        monkeypatch.setattr(config, "ADMIN_USER_ID", 777)
        seed_user(app_database, 777, plan="admin")

        credit(777, 10, "t:admin", kind=balance.CoinKind.PAID)

        assert user_state(app_database, 777)["plan"] == "admin"

    def test_unknown_user_is_reported_and_nothing_is_written(self, app_database):
        with pytest.raises(balance.UserNotFound):
            credit(404, 5, "t:missing")

        assert ledger_rows(app_database) == []

    @pytest.mark.parametrize("amount", [0, -3, True, 2.5, "7"])
    def test_invalid_amounts_are_rejected(self, app_database, amount):
        seed_user(app_database, 1)

        with pytest.raises(balance.InvalidBalanceRequest):
            credit(1, amount, "t:bad-amount")

        assert ledger_rows(app_database) == []

    @pytest.mark.parametrize(
        "key", ["", "has space", "中文", "x" * 129, "refund:anything"]
    )
    def test_invalid_op_keys_are_rejected(self, app_database, key):
        seed_user(app_database, 1)

        with pytest.raises(balance.InvalidBalanceRequest):
            credit(1, 1, key)


class TestIdempotency:
    def test_replaying_a_credit_does_not_credit_twice(self, app_database):
        seed_user(app_database, 1)

        first = credit(1, 5, "t:once")
        replay = credit(1, 5, "t:once")

        assert first.applied is True and replay.applied is False
        assert replay.balance_free == first.balance_free == 5
        assert user_state(app_database, 1)["free"] == 5
        assert ledger_keys(app_database) == ["t:once"]

    def test_replaying_a_debit_does_not_debit_twice(self, app_database):
        seed_user(app_database, 1, free=10)

        first = debit(1, 4, "t:spend")
        replay = debit(1, 4, "t:spend")

        assert first.applied is True and replay.applied is False
        assert user_state(app_database, 1)["free"] == 6
        assert len(ledger_rows(app_database)) == 1

    def test_a_debit_replays_even_when_the_balance_has_since_run_out(self, app_database):
        seed_user(app_database, 1, free=4)
        debit(1, 4, "t:spend")

        replay = debit(1, 4, "t:spend")

        assert replay.applied is False
        assert user_state(app_database, 1)["free"] == 0

    def test_get_operation_returns_the_recorded_result(self, app_database):
        seed_user(app_database, 1, free=1, paid=3)
        debit(1, 2, "t:spend")

        found = run(balance.get_operation("t:spend"))

        assert found is not None and found.applied is False
        assert (found.delta_free, found.delta_paid) == (-1, -1)
        assert found.kind is balance.LedgerKind.DEBIT
        assert run(balance.get_operation("t:unknown")) is None

    @pytest.mark.parametrize(
        ("request_user", "request_amount", "request_kind"),
        [
            (2, 5, balance.CoinKind.FREE),
            (1, 6, balance.CoinKind.FREE),
            (1, 5, balance.CoinKind.PAID),
        ],
        ids=["other-user", "other-amount", "other-kind"],
    )
    def test_same_op_key_with_different_credit_parameters_conflicts(
        self, app_database, request_user, request_amount, request_kind
    ):
        seed_user(app_database, 1)
        seed_user(app_database, 2)
        credit(1, 5, "t:id")

        with pytest.raises(balance.OperationConflict):
            credit(request_user, request_amount, "t:id", kind=request_kind)

        assert ledger_keys(app_database) == ["t:id"]
        assert user_state(app_database, 1)["free"] == 5
        assert user_state(app_database, 2)["free"] == 0

    def test_same_op_key_for_a_credit_and_a_debit_conflicts(self, app_database):
        seed_user(app_database, 1, free=10)
        credit(1, 5, "t:id")

        with pytest.raises(balance.OperationConflict):
            debit(1, 5, "t:id")

        assert user_state(app_database, 1)["free"] == 15

    def test_same_op_key_with_a_different_debit_amount_conflicts(self, app_database):
        seed_user(app_database, 1, free=10)
        debit(1, 3, "t:id")

        with pytest.raises(balance.OperationConflict):
            debit(1, 4, "t:id")

        assert user_state(app_database, 1)["free"] == 7

    def test_concurrent_replays_of_one_op_key_apply_it_once(self, app_database):
        seed_user(app_database, 1)

        async def scenario():
            return await gather_all(
                *[
                    balance.credit_standalone(1, 5, op_key="t:race", reason="test")
                    for _ in range(6)
                ]
            )

        results = run(scenario())

        assert all(not isinstance(item, Exception) for item in results), results
        assert sorted(item.applied for item in results) == [False] * 5 + [True]
        assert user_state(app_database, 1)["free"] == 5
        assert ledger_keys(app_database) == ["t:race"]

    def test_one_op_key_used_by_two_users_concurrently_credits_only_one(self, app_database):
        # 不同用户的两个事务不共享 user 行锁，只有 op_key 的唯一约束能分出胜负；
        # 输家必须得到明确的冲突异常，而不是死锁或重复入账。
        seed_user(app_database, 1)
        seed_user(app_database, 2)

        async def scenario():
            return await gather_all(
                balance.credit_standalone(1, 5, op_key="t:shared", reason="test"),
                balance.credit_standalone(2, 5, op_key="t:shared", reason="test"),
            )

        results = run(scenario())

        winners = [item for item in results if not isinstance(item, Exception)]
        losers = [item for item in results if isinstance(item, Exception)]
        assert len(winners) == 1 and winners[0].applied is True
        assert len(losers) == 1 and isinstance(losers[0], balance.OperationConflict)
        assert total_coins(app_database, 1) + total_coins(app_database, 2) == 5
        assert ledger_keys(app_database) == ["t:shared"]


class TestDebit:
    def test_free_coins_are_spent_before_paid_coins(self, app_database):
        seed_user(app_database, 1, free=2, paid=10)

        result = debit(1, 5, "t:split")

        assert (result.delta_free, result.delta_paid) == (-2, -3)
        assert (result.balance_free, result.balance_paid) == (0, 7)
        assert user_state(app_database, 1) == {"free": 0, "paid": 7, "plan": "paid"}

    def test_spending_the_last_paid_coin_returns_the_plan_to_free(self, app_database):
        seed_user(app_database, 1, free=0, paid=3)

        debit(1, 3, "t:last")

        assert user_state(app_database, 1) == {"free": 0, "paid": 0, "plan": "free"}

    def test_insufficient_balance_changes_nothing(self, app_database):
        seed_user(app_database, 1, free=2, paid=1)

        with pytest.raises(balance.InsufficientBalance) as caught:
            debit(1, 4, "t:too-much")

        assert caught.value.balance_total == 3
        assert caught.value.requested == 4
        assert user_state(app_database, 1) == {"free": 2, "paid": 1, "plan": "paid"}
        assert ledger_rows(app_database) == []

    def test_a_failed_debit_does_not_consume_the_op_key(self, app_database):
        seed_user(app_database, 1, free=1)
        with pytest.raises(balance.InsufficientBalance):
            debit(1, 3, "t:retry")
        credit(1, 5, "t:topup")

        result = debit(1, 3, "t:retry")

        assert result.applied is True
        assert user_state(app_database, 1)["free"] == 3

    def test_unknown_user_is_reported(self, app_database):
        with pytest.raises(balance.UserNotFound):
            debit(404, 1, "t:missing")

    def test_concurrent_debits_never_overdraw(self, app_database):
        seed_user(app_database, 1, free=4, paid=6)

        async def scenario():
            return await gather_all(
                *[
                    balance.debit_standalone(1, 3, op_key=f"t:spend:{index}", reason="test")
                    for index in range(8)
                ]
            )

        results = run(scenario())

        succeeded = [item for item in results if not isinstance(item, Exception)]
        failed = [item for item in results if isinstance(item, Exception)]
        assert len(succeeded) == 3  # 10 个币，每次 3 个
        assert all(isinstance(item, balance.InsufficientBalance) for item in failed)
        assert user_state(app_database, 1) == {"free": 0, "paid": 1, "plan": "paid"}
        assert len(ledger_rows(app_database)) == 3
        # 账本里的每一行都是当时真实的余额，没有哪一行出现负数。
        assert all(row["balance_free"] >= 0 and row["balance_paid"] >= 0 for row in ledger_rows(app_database))


class TestRefund:
    def test_refund_returns_the_coins_along_the_original_route(self, app_database):
        seed_user(app_database, 1, free=2, paid=5)
        debit(1, 6, "t:spend")
        assert user_state(app_database, 1) == {"free": 0, "paid": 1, "plan": "paid"}

        result = run(balance.refund_standalone("t:spend"))

        assert result.applied is True
        assert result.kind is balance.LedgerKind.REFUND
        assert (result.delta_free, result.delta_paid) == (2, 4)
        assert result.ref == "t:spend"
        assert user_state(app_database, 1) == {"free": 2, "paid": 5, "plan": "paid"}
        assert ledger_keys(app_database) == ["t:spend", "refund:t:spend"]

    def test_refund_restores_the_paid_plan_when_the_last_paid_coin_had_been_spent(
        self, app_database
    ):
        seed_user(app_database, 1, free=0, paid=3)
        debit(1, 3, "t:spend")
        assert user_state(app_database, 1)["plan"] == "free"

        run(balance.refund_standalone("t:spend"))

        assert user_state(app_database, 1) == {"free": 0, "paid": 3, "plan": "paid"}

    def test_refunding_twice_refunds_once(self, app_database):
        seed_user(app_database, 1, free=5)
        debit(1, 5, "t:spend")

        first = run(balance.refund_standalone("t:spend"))
        second = run(balance.refund_standalone("t:spend"))

        assert first.applied is True and second.applied is False
        assert user_state(app_database, 1)["free"] == 5

    def test_concurrent_refunds_refund_once(self, app_database):
        seed_user(app_database, 1, free=5)
        debit(1, 5, "t:spend")

        async def scenario():
            return await gather_all(
                *[balance.refund_standalone("t:spend") for _ in range(4)]
            )

        results = run(scenario())

        assert all(not isinstance(item, Exception) for item in results), results
        assert sorted(item.applied for item in results) == [False] * 3 + [True]
        assert user_state(app_database, 1)["free"] == 5

    def test_refunding_an_unknown_operation_is_rejected(self, app_database):
        seed_user(app_database, 1)

        with pytest.raises(balance.RefundRejected):
            run(balance.refund_standalone("t:never-happened"))

        assert ledger_rows(app_database) == []

    def test_refunding_a_debit_that_failed_is_rejected(self, app_database):
        seed_user(app_database, 1, free=1)
        with pytest.raises(balance.InsufficientBalance):
            debit(1, 5, "t:failed")

        with pytest.raises(balance.RefundRejected):
            run(balance.refund_standalone("t:failed"))

        assert user_state(app_database, 1)["free"] == 1
        assert ledger_rows(app_database) == []

    def test_only_debits_can_be_refunded(self, app_database):
        seed_user(app_database, 1)
        credit(1, 5, "t:credit")

        with pytest.raises(balance.RefundRejected):
            run(balance.refund_standalone("t:credit"))

        assert user_state(app_database, 1)["free"] == 5


class TestTransactionOwnership:
    def test_business_state_and_balance_commit_or_roll_back_together(self, app_database):
        seed_user(app_database, 1, free=10)

        async def scenario(*, fail: bool):
            async with mysql_connection.transaction() as connection:
                await connection.exec_driver_sql(
                    "INSERT INTO user_task (user_id, task_id) VALUES (1, 1)"
                )
                await balance.debit(connection, 1, 3, op_key="t:buy", reason="test")
                if fail:
                    raise RuntimeError("业务步骤失败")

        with pytest.raises(RuntimeError):
            run(scenario(fail=True))
        assert fetch_scalar(app_database, "SELECT COUNT(*) FROM user_task") == 0
        assert user_state(app_database, 1)["free"] == 10
        assert ledger_rows(app_database) == []

        run(scenario(fail=False))
        assert fetch_scalar(app_database, "SELECT COUNT(*) FROM user_task") == 1
        assert user_state(app_database, 1)["free"] == 7
        assert ledger_keys(app_database) == ["t:buy"]

    def test_a_caller_may_catch_insufficient_balance_and_keep_using_the_transaction(
        self, app_database
    ):
        seed_user(app_database, 1, free=2)

        async def scenario():
            async with mysql_connection.transaction() as connection:
                try:
                    await balance.debit(connection, 1, 5, op_key="t:big", reason="test")
                except balance.InsufficientBalance:
                    await balance.debit(connection, 1, 2, op_key="t:small", reason="test")

        run(scenario())

        assert user_state(app_database, 1)["free"] == 0
        assert ledger_keys(app_database) == ["t:small"]

    def test_a_duplicate_key_conflict_inside_a_transaction_leaves_it_usable(self, app_database):
        seed_user(app_database, 1)
        seed_user(app_database, 2)
        credit(1, 5, "t:taken")

        async def scenario():
            async with mysql_connection.transaction() as connection:
                with pytest.raises(balance.OperationConflict):
                    await balance.credit(connection, 2, 5, op_key="t:taken", reason="test")
                await balance.credit(connection, 2, 1, op_key="t:other", reason="test")

        run(scenario())

        assert user_state(app_database, 2)["free"] == 1


class TestLedgerReconciliation:
    def test_balances_and_ledger_agree_after_a_mixed_sequence(self, app_database):
        seed_user(app_database, 1, free=3, paid=0)
        seed_user(app_database, 2, free=0, paid=0)
        credit(1, 10, "t:a")
        credit(1, 20, "t:b", kind=balance.CoinKind.PAID)
        debit(1, 15, "t:c")
        run(balance.refund_standalone("t:c"))
        debit(1, 7, "t:d")
        credit(2, 4, "t:e")
        debit(2, 1, "t:f")

        for user_id in (1, 2):
            rows = ledger_rows(app_database, user_id)
            state = user_state(app_database, user_id)
            # 最新一行的余额等于 user 表。
            assert (rows[-1]["balance_free"], rows[-1]["balance_paid"]) == (
                state["free"],
                state["paid"],
            )
            # 相邻两行首尾相接；第一行隐含了开户前的余额。
            for previous, current in zip(rows, rows[1:]):
                assert current["balance_free"] - current["delta_free"] == previous["balance_free"]
                assert current["balance_paid"] - current["delta_paid"] == previous["balance_paid"]

        sums = fetch(
            app_database,
            "SELECT SUM(delta_free) AS f, SUM(delta_paid) AS p FROM coin_ledger WHERE user_id = 1",
        )[0]
        # 开户余额 3 免费 + 变动之和 = 当前余额
        assert 3 + int(sums["f"]) == user_state(app_database, 1)["free"]
        assert 0 + int(sums["p"]) == user_state(app_database, 1)["paid"]


class TestAudit:
    def test_a_clean_ledger_reports_no_drift(self, app_database):
        seed_user(app_database, 1, free=3)
        credit(1, 10, "t:a")
        debit(1, 6, "t:b")
        run(balance.refund_standalone("t:b"))
        run(stake_reward_pool.credit_pool_standalone(1, op_key="p:a", reason="test"))

        report = run(balance.audit_ledger())

        assert report.clean, report

    def test_a_balance_changed_behind_the_ledgers_back_is_reported(self, app_database):
        seed_user(app_database, 1, free=3)
        seed_user(app_database, 2)
        credit(1, 10, "t:a")
        credit(1, 1, "t:b")
        credit(2, 5, "t:c")
        execute(app_database, "UPDATE `user` SET coins = coins + 7 WHERE id = 1")

        report = run(balance.audit_ledger())

        assert report.balance_mismatches == (1,)
        assert report.broken_chains == ()
        assert not report.clean

    def test_a_gap_inside_the_chain_is_reported_even_after_later_entries(self, app_database):
        seed_user(app_database, 1)
        credit(1, 10, "t:a")
        execute(app_database, "UPDATE `user` SET coins = coins - 4 WHERE id = 1")
        credit(1, 1, "t:b")

        report = run(balance.audit_ledger())

        # 后来的入账读到的是真实余额，末行与 user 表又对上了；缺口只在账本相邻两行之间看得出来。
        assert report.balance_mismatches == ()
        assert report.broken_chains == (1,)
        assert not report.clean

    def test_a_pool_changed_behind_the_ledgers_back_is_reported(self, app_database):
        run(stake_reward_pool.credit_pool_standalone(1, op_key="p:a", reason="test"))
        execute(app_database, "UPDATE stake_reward_pool SET balance = balance + 5 WHERE id = 1")

        assert run(balance.audit_ledger()).pool_mismatch is True

    def test_an_empty_ledger_is_clean(self, app_database):
        seed_user(app_database, 1, free=5)

        assert run(balance.audit_ledger()).clean


def test_ledger_keeps_rows_for_users_that_no_longer_exist(app_database):
    seed_user(app_database, 1)
    credit(1, 5, "t:a")
    execute(app_database, "DELETE FROM `user` WHERE id = 1")

    assert ledger_keys(app_database) == ["t:a"]
    assert fetch_scalar(app_database, "SELECT COUNT(*) FROM coin_ledger WHERE user_id = 1") == 1
