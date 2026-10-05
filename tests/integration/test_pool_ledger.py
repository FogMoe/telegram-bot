"""奖池账本：贡献与扣减带 op_key，重放不重复，参数不一致报冲突（真实 MySQL）。"""

from decimal import Decimal

import pytest
from economy_support import gather_all, pool_balance, pool_rows, seed_user
from mysql_support import execute, run

from fogmoe_telegram_bot.core import balance, mysql_connection, stake_reward_pool


def credit(amount, key, **kwargs):
    return run(
        stake_reward_pool.credit_pool_standalone(amount, op_key=key, reason="test", **kwargs)
    )


def test_credit_adds_to_the_pool_and_records_the_balance_after(app_database):
    execute(app_database, "UPDATE stake_reward_pool SET balance = 10 WHERE id = 1")

    result = credit(Decimal("2.50"), "p:one", ref="spend:1")

    assert result.applied is True
    assert (result.delta, result.balance) == (Decimal("2.50"), Decimal("12.50"))
    assert pool_balance(app_database) == Decimal("12.50")
    assert [(row["op_key"], row["kind"], row["ref"]) for row in pool_rows(app_database)] == [
        ("p:one", "credit", "spend:1")
    ]


def test_replaying_a_contribution_does_not_add_twice(app_database):
    credit(Decimal("1.00"), "p:once")

    replay = credit(Decimal("1.00"), "p:once")

    assert replay.applied is False
    assert pool_balance(app_database) == Decimal("1.00")
    assert len(pool_rows(app_database)) == 1


def test_concurrent_contributions_with_one_op_key_apply_once(app_database):
    async def scenario():
        return await gather_all(
            *[
                stake_reward_pool.credit_pool_standalone(
                    Decimal("0.20"), op_key="p:race", reason="test"
                )
                for _ in range(6)
            ]
        )

    results = run(scenario())

    assert all(not isinstance(item, Exception) for item in results), results
    assert sorted(item.applied for item in results) == [False] * 5 + [True]
    assert pool_balance(app_database) == Decimal("0.20")


def test_concurrent_contributions_with_distinct_keys_all_land(app_database):
    async def scenario():
        return await gather_all(
            *[
                stake_reward_pool.credit_pool_standalone(
                    Decimal("0.20"), op_key=f"p:many:{index}", reason="test"
                )
                for index in range(10)
            ]
        )

    results = run(scenario())

    assert all(not isinstance(item, Exception) for item in results), results
    assert pool_balance(app_database) == Decimal("2.00")
    # 每一行记录的「变动后余额」都互不相同：写入是串行的，没有丢失更新。
    assert len({row["balance_after"] for row in pool_rows(app_database)}) == 10


def test_same_op_key_with_a_different_amount_or_kind_conflicts(app_database):
    execute(app_database, "UPDATE stake_reward_pool SET balance = 10 WHERE id = 1")
    credit(Decimal("1.00"), "p:id")

    with pytest.raises(balance.OperationConflict):
        credit(Decimal("2.00"), "p:id")
    with pytest.raises(balance.OperationConflict):
        run(
            stake_reward_pool.debit_pool_standalone(
                Decimal("1.00"), op_key="p:id", reason="test"
            )
        )

    assert pool_balance(app_database) == Decimal("11.00")
    assert len(pool_rows(app_database)) == 1


def test_debit_reduces_the_pool_and_refuses_to_overdraw(app_database):
    execute(app_database, "UPDATE stake_reward_pool SET balance = 3 WHERE id = 1")

    result = run(
        stake_reward_pool.debit_pool_standalone(Decimal("2.00"), op_key="p:pay", reason="test")
    )
    assert (result.delta, result.balance) == (Decimal("-2.00"), Decimal("1.00"))

    with pytest.raises(stake_reward_pool.PoolInsufficient):
        run(
            stake_reward_pool.debit_pool_standalone(
                Decimal("5.00"), op_key="p:too-much", reason="test"
            )
        )
    assert pool_balance(app_database) == Decimal("1.00")
    assert [row["op_key"] for row in pool_rows(app_database)] == ["p:pay"]


@pytest.mark.parametrize("amount", [0, -1, Decimal("0.001"), "abc", Decimal("NaN")])
def test_invalid_pool_amounts_are_rejected(app_database, amount):
    with pytest.raises(balance.InvalidBalanceRequest):
        credit(amount, "p:bad")

    assert pool_rows(app_database) == []


def test_share_of_spend_uses_a_key_derived_from_the_spend(app_database):
    async def contribute():
        return await stake_reward_pool.credit_share_of_spend_standalone(
            5, spend_op_key="chat:1:2"
        )

    first = run(contribute())
    replay = run(contribute())

    assert first is not None and first.delta == Decimal("1.00")
    assert replay is not None and replay.applied is False
    assert pool_balance(app_database) == Decimal("1.00")
    assert [(row["op_key"], row["ref"]) for row in pool_rows(app_database)] == [
        ("pool:chat:1:2", "chat:1:2")
    ]


def test_contribution_rolls_back_with_the_callers_transaction(app_database):
    seed_user(app_database, 1, free=5)

    async def scenario():
        async with mysql_connection.transaction() as connection:
            await balance.debit(connection, 1, 5, op_key="spend:1", reason="test")
            await stake_reward_pool.credit_share_of_spend(connection, 5, spend_op_key="spend:1")
            raise RuntimeError("回滚")

    with pytest.raises(RuntimeError):
        run(scenario())

    assert pool_balance(app_database) == Decimal("0")
    assert pool_rows(app_database) == []


def test_pool_ledger_balances_chain_to_the_current_pool_balance(app_database):
    execute(app_database, "UPDATE stake_reward_pool SET balance = 4 WHERE id = 1")
    credit(Decimal("1.00"), "p:a")
    credit(Decimal("0.50"), "p:b")
    run(stake_reward_pool.debit_pool_standalone(Decimal("2.00"), op_key="p:c", reason="test"))

    rows = pool_rows(app_database)

    assert rows[-1]["balance_after"] == pool_balance(app_database)
    for previous, current in zip(rows, rows[1:]):
        assert current["balance_after"] - current["delta"] == previous["balance_after"]
