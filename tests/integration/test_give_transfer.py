"""/give 转账：扣款（含手续费）、入账、每日次数在同一个事务里（真实 MySQL）。"""

from datetime import date, datetime

import pytest
from economy_support import (
    gather_all,
    ledger_keys,
    ledger_rows,
    make_command_update,
    make_context,
    seed_user,
    user_state,
)
from mysql_support import fetch, fetch_scalar, run

from core import balance, mysql_connection, sql
from features.economy import coins
from features.economy.coins import GiveStatus

TODAY = date(2026, 10, 5)
give_command = coins.give_command.__wrapped__


def total(url, user_id):
    state = user_state(url, user_id)
    return state["free"] + state["paid"]


def daily_count(url, user_id=1, day=TODAY):
    value = fetch_scalar(
        url,
        "SELECT give_count FROM user_give_daily WHERE user_id = %s AND give_date = %s",
        (user_id, day),
    )
    return value or 0


def transfer(sender, recipient, amount, message_id, *, chat_id=9, today=TODAY):
    return coins.transfer_coins(
        sender,
        recipient,
        amount,
        sender_op_key=coins.give_op_key(chat_id, message_id),
        recipient_op_key=coins.give_recipient_op_key(chat_id, message_id),
        today=today,
    )


def seed_pair(url, *, sender_coins=100, recipient_coins=0):
    seed_user(url, 1, free=sender_coins, name="alice")
    seed_user(url, 2, free=recipient_coins, name="bob")


class TestTransfer:
    def test_a_transfer_debits_amount_plus_fee_and_credits_the_amount(self, app_database):
        seed_pair(app_database)

        outcome = run(transfer(1, 2, 10, 11))

        assert outcome.status is GiveStatus.GIVEN
        assert total(app_database, 1) == 88  # 10 + 手续费 2
        assert total(app_database, 2) == 10
        assert daily_count(app_database) == 1
        rows = ledger_rows(app_database)
        assert [(row["op_key"], row["user_id"], row["kind"]) for row in rows] == [
            ("give:9:11", 1, "debit"),
            ("give:9:11:recv", 2, "credit"),
        ]
        assert rows[0]["ref"] == "to:2" and rows[1]["ref"] == "from:1"
        assert run(balance.audit_ledger()).clean

    def test_a_single_coin_has_no_fee(self, app_database):
        seed_pair(app_database)

        run(transfer(1, 2, 1, 11))

        assert total(app_database, 1) == 99
        assert total(app_database, 2) == 1

    def test_the_fee_comes_from_free_coins_first(self, app_database):
        seed_user(app_database, 1, free=5, paid=50, name="alice")
        seed_user(app_database, 2, name="bob")

        run(transfer(1, 2, 20, 11))  # 20 + 手续费 4 = 24

        assert user_state(app_database, 1) == {"free": 0, "paid": 31, "plan": "paid"}
        # 收款人收到的是免费金币。
        assert user_state(app_database, 2)["free"] == 20

    def test_an_insufficient_balance_changes_nothing(self, app_database):
        seed_pair(app_database, sender_coins=11)

        outcome = run(transfer(1, 2, 10, 11))  # 需要 12

        assert (outcome.status, outcome.balance_total) == (GiveStatus.INSUFFICIENT, 11)
        assert total(app_database, 1) == 11
        assert total(app_database, 2) == 0
        assert daily_count(app_database) == 0
        assert ledger_rows(app_database) == []

    def test_a_missing_recipient_a_missing_sender_and_a_self_gift_are_refused(self, app_database):
        seed_pair(app_database)

        missing_recipient = run(transfer(1, None, 10, 11))
        self_gift = run(transfer(1, 1, 10, 12))
        missing_sender = run(transfer(404, 2, 10, 13))

        assert missing_recipient.status is GiveStatus.RECIPIENT_NOT_FOUND
        assert self_gift.status is GiveStatus.SELF
        assert missing_sender.status is GiveStatus.NOT_REGISTERED
        assert total(app_database, 1) == 100
        assert ledger_rows(app_database) == []

    def test_the_same_command_delivered_twice_transfers_once(self, app_database):
        seed_pair(app_database)

        first = run(transfer(1, 2, 10, 11))
        replay = run(transfer(1, 2, 10, 11))

        assert (first.status, replay.status) == (GiveStatus.GIVEN, GiveStatus.REPLAYED)
        assert total(app_database, 1) == 88
        assert total(app_database, 2) == 10
        assert daily_count(app_database) == 1
        assert len(ledger_rows(app_database)) == 2

    def test_a_replay_of_the_last_allowed_gift_is_not_mistaken_for_the_limit(self, app_database):
        seed_pair(app_database, sender_coins=1000)
        for message_id in range(1, 6):
            run(transfer(1, 2, 10, message_id))

        replay = run(transfer(1, 2, 10, 5))

        assert replay.status is GiveStatus.REPLAYED
        assert daily_count(app_database) == 5

    def test_concurrent_deliveries_of_one_command_transfer_once(self, app_database):
        seed_pair(app_database)

        async def scenario():
            return await gather_all(*[transfer(1, 2, 10, 11) for _ in range(6)])

        results = run(scenario())

        assert all(not isinstance(item, Exception) for item in results), results
        statuses = [item.status for item in results]
        assert statuses.count(GiveStatus.GIVEN) == 1
        assert statuses.count(GiveStatus.REPLAYED) == 5
        assert total(app_database, 1) == 88
        assert total(app_database, 2) == 10

    def test_the_daily_limit_holds_under_concurrent_commands(self, app_database):
        seed_pair(app_database, sender_coins=1000)

        async def scenario():
            return await gather_all(*[transfer(1, 2, 10, message_id) for message_id in range(8)])

        results = run(scenario())

        assert all(not isinstance(item, Exception) for item in results), results
        statuses = [item.status for item in results]
        assert statuses.count(GiveStatus.GIVEN) == coins.GIVE_DAILY_LIMIT
        assert statuses.count(GiveStatus.DAILY_LIMIT) == 8 - coins.GIVE_DAILY_LIMIT
        assert daily_count(app_database) == coins.GIVE_DAILY_LIMIT
        assert total(app_database, 1) == 1000 - 5 * 12
        assert total(app_database, 2) == 5 * 10
        assert len(ledger_rows(app_database)) == 10

    def test_the_limit_resets_on_the_next_day(self, app_database):
        seed_pair(app_database, sender_coins=1000)
        for message_id in range(5):
            run(transfer(1, 2, 10, message_id))
        refused = run(transfer(1, 2, 10, 50))

        tomorrow = run(transfer(1, 2, 10, 51, today=date(2026, 10, 6)))

        assert refused.status is GiveStatus.DAILY_LIMIT
        assert tomorrow.status is GiveStatus.GIVEN

    def test_concurrent_transfers_cannot_overspend_the_sender(self, app_database):
        seed_pair(app_database, sender_coins=30)

        async def scenario():
            return await gather_all(*[transfer(1, 2, 10, message_id) for message_id in range(5)])

        results = run(scenario())

        assert all(not isinstance(item, Exception) for item in results), results
        statuses = [item.status for item in results]
        assert statuses.count(GiveStatus.GIVEN) == 2  # 每笔 12，30 只够两笔
        assert statuses.count(GiveStatus.INSUFFICIENT) == 3
        assert total(app_database, 1) == 6
        assert total(app_database, 2) == 20

    def test_gifts_in_opposite_directions_do_not_deadlock(self, app_database, monkeypatch):
        deadlocks = []
        real_is_deadlock = sql.is_deadlock_error

        def recording_is_deadlock(exc):
            found = real_is_deadlock(exc)
            if found:
                deadlocks.append(exc)
            return found

        monkeypatch.setattr(sql, "is_deadlock_error", recording_is_deadlock)
        seed_pair(app_database, sender_coins=500, recipient_coins=500)

        async def scenario():
            jobs = []
            for index in range(4):
                jobs.append(transfer(1, 2, 10, 100 + index))
                jobs.append(transfer(2, 1, 10, 200 + index))
            return await gather_all(*jobs)

        results = run(scenario())

        assert all(not isinstance(item, Exception) for item in results), results
        assert {item.status for item in results} == {GiveStatus.GIVEN}
        assert deadlocks == []
        # 双方各转出 4 笔、收到 4 笔：净变化只有手续费。
        assert total(app_database, 1) == 500 - 4 * 12 + 4 * 10
        assert total(app_database, 2) == 500 - 4 * 12 + 4 * 10

    def test_a_failure_while_crediting_the_recipient_rolls_everything_back(
        self, app_database, monkeypatch
    ):
        seed_pair(app_database)
        real_credit = balance.credit

        async def failing_credit(*args, **kwargs):
            raise RuntimeError("入账失败")

        monkeypatch.setattr(balance, "credit", failing_credit)
        with pytest.raises(RuntimeError):
            run(transfer(1, 2, 10, 11))
        monkeypatch.setattr(balance, "credit", real_credit)

        assert total(app_database, 1) == 100
        assert total(app_database, 2) == 0
        assert daily_count(app_database) == 0
        assert ledger_rows(app_database) == []

        # 重试同一条命令可以成功，而不是被判成「已经处理过」。
        retry = run(transfer(1, 2, 10, 11))
        assert retry.status is GiveStatus.GIVEN


class TestGiveCommand:
    def give(self, url, args, *, message_id=11, user_id=1):
        update = make_command_update(user_id=user_id, chat_id=9, message_id=message_id)
        context = make_context()
        context.args = list(args)
        run(give_command(update, context))
        return update.message.reply_text.texts

    def test_a_successful_gift_reports_the_fee(self, app_database):
        seed_pair(app_database)

        texts = self.give(app_database, ["bob", "10"])

        assert texts == ["成功赠送 10 枚硬币给用户 bob，手续费 2 枚硬币。"]
        assert total(app_database, 2) == 10

    def test_an_insufficient_balance_shows_the_total_cost(self, app_database):
        seed_pair(app_database, sender_coins=5)

        texts = self.give(app_database, ["bob", "10"])

        assert texts == ["您的硬币不足，当前硬币：5，需要：12"]

    def test_an_unknown_name_is_reported(self, app_database):
        seed_pair(app_database)

        texts = self.give(app_database, ["nobody", "10"])

        assert texts == ["未找到用户名为 'nobody' 的用户。"]

    def test_the_daily_limit_message_is_shown(self, app_database):
        seed_pair(app_database, sender_coins=1000)
        today = datetime.now().date()
        for index in range(coins.GIVE_DAILY_LIMIT):
            run(transfer(1, 2, 1, index, today=today))

        texts = self.give(app_database, ["bob", "10"], message_id=77)

        assert texts == ["您今天的赠送次数已达上限（5次），请明天再试。"]

    def test_a_redelivered_command_is_confirmed_without_transferring_again(self, app_database):
        seed_pair(app_database)

        self.give(app_database, ["bob", "10"], message_id=11)
        texts = self.give(app_database, ["bob", "10"], message_id=11)

        assert texts == ["成功赠送 10 枚硬币给用户 bob，手续费 2 枚硬币。"]
        assert total(app_database, 1) == 88
        assert total(app_database, 2) == 10
        assert len(ledger_rows(app_database)) == 2

    def test_a_non_positive_amount_is_rejected_before_touching_the_database(self, app_database):
        seed_pair(app_database)

        texts = self.give(app_database, ["bob", "0"])

        assert texts == ["赠送数量必须为正整数！"]
        assert ledger_keys(app_database) == []


def test_transfer_state_is_visible_in_user_give_daily(app_database):
    seed_pair(app_database)
    run(transfer(1, 2, 10, 11))

    rows = fetch(app_database, "SELECT user_id, give_date, give_count FROM user_give_daily")

    assert rows == [{"user_id": 1, "give_date": TODAY, "give_count": 1}]


class TestLockUsers:
    def test_every_requested_user_is_locked_once_and_balances_are_returned(self, app_database):
        seed_user(app_database, 1, free=3, paid=1)
        seed_user(app_database, 2, free=5)

        async def scenario():
            async with mysql_connection.transaction() as connection:
                return await balance.lock_users(connection, [2, 1, 2])

        locked = run(scenario())

        assert locked == {
            1: balance.UserBalances(free=3, paid=1),
            2: balance.UserBalances(free=5, paid=0),
        }

    def test_a_missing_user_is_named_in_the_error(self, app_database):
        seed_user(app_database, 1)

        async def scenario():
            async with mysql_connection.transaction() as connection:
                await balance.lock_users(connection, [404, 1])

        with pytest.raises(balance.UserNotFound) as raised:
            run(scenario())

        assert raised.value.user_id == 404
