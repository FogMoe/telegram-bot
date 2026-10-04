"""代币兑换、AI 善意赠币与贿赂：扣款/入账与业务记录同事务，重复执行与并发不重复变动（真实 MySQL）。"""

import asyncio

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
from mysql_support import execute, fetch, fetch_scalar, run

from core import balance, db, mysql_connection, process_user
from features.ai.tools import context as tool_context
from features.ai.tools import user_tools
from features.crypto import swap_fogmoe_solana_token as swap
from features.crypto.swap_fogmoe_solana_token import SwapStatus
from features.economy import bribe
from features.economy.bribe import BribeStatus

swap_command = swap.swap_command.__wrapped__
WALLET = "5iz3epFDf9SKvLNHWQ42f4wMMrENaudE9eMkxfBLFd2n"


def total(url, user_id=1):
    state = user_state(url, user_id)
    return state["free"] + state["paid"]


def fail_after(monkeypatch, name):
    real = getattr(balance, name)

    async def wrapper(*args, **kwargs):
        await real(*args, **kwargs)
        raise RuntimeError("injected failure")

    monkeypatch.setattr(balance, name, wrapper)
    return real


class TestSwapRequests:
    def submit(self, message_id=11, amount=10000, *, user_id=1):
        return swap.submit_swap_request(
            user_id,
            "someone",
            WALLET,
            amount,
            op_key=swap.swap_op_key(9, message_id),
        )

    def requests(self, url):
        return fetch(url, "SELECT user_id, amount, wallet_address, status FROM token_swap_requests")

    def test_the_debit_and_the_request_are_recorded_together(self, app_database):
        seed_user(app_database, 1, free=12000)

        outcome = run(self.submit())

        assert outcome.status is SwapStatus.SUBMITTED
        assert total(app_database) == 2000
        assert self.requests(app_database) == [
            {"user_id": 1, "amount": 10000, "wallet_address": WALLET, "status": "pending"}
        ]
        assert ledger_keys(app_database) == ["swap:9:11"]

    def test_an_insufficient_balance_records_no_request(self, app_database):
        seed_user(app_database, 1, free=9999)

        outcome = run(self.submit())

        assert outcome.status is SwapStatus.INSUFFICIENT
        assert self.requests(app_database) == []
        assert total(app_database) == 9999
        assert ledger_rows(app_database) == []

    def test_a_pending_request_blocks_a_second_one_without_charging(self, app_database):
        seed_user(app_database, 1, free=30000)
        run(self.submit(11))

        outcome = run(self.submit(12))

        assert outcome.status is SwapStatus.PENDING_EXISTS
        assert outcome.pending["amount"] == 10000
        assert total(app_database) == 20000
        assert len(self.requests(app_database)) == 1

    def test_a_request_that_was_processed_does_not_block_the_next_one(self, app_database):
        seed_user(app_database, 1, free=30000)
        run(self.submit(11))
        execute(app_database, "UPDATE token_swap_requests SET status = 'completed'")

        outcome = run(self.submit(12))

        assert outcome.status is SwapStatus.SUBMITTED
        assert total(app_database) == 10000

    def test_the_same_command_delivered_twice_charges_once(self, app_database):
        seed_user(app_database, 1, free=30000)

        run(self.submit(11))
        execute(app_database, "UPDATE token_swap_requests SET status = 'completed'")
        replay = run(self.submit(11))

        assert replay.status is SwapStatus.REPLAYED
        assert total(app_database) == 20000
        assert len(self.requests(app_database)) == 1

    def test_concurrent_requests_by_one_user_create_one_request(self, app_database):
        seed_user(app_database, 1, free=50000)

        async def scenario():
            return await gather_all(*[self.submit(message_id) for message_id in range(5)])

        results = run(scenario())

        assert all(not isinstance(item, Exception) for item in results), results
        statuses = [item.status for item in results]
        assert statuses.count(SwapStatus.SUBMITTED) == 1
        assert statuses.count(SwapStatus.PENDING_EXISTS) == 4
        assert total(app_database) == 40000
        assert len(self.requests(app_database)) == 1

    def test_a_failure_after_the_debit_leaves_no_request_and_no_charge(
        self, app_database, monkeypatch
    ):
        seed_user(app_database, 1, free=12000)
        fail_after(monkeypatch, "debit")

        with pytest.raises(RuntimeError):
            run(self.submit())

        assert total(app_database) == 12000
        assert self.requests(app_database) == []
        assert ledger_rows(app_database) == []

    def test_the_command_confirms_a_request(self, app_database):
        seed_user(app_database, 1, free=12000)
        update = make_command_update(user_id=1, chat_id=9, message_id=11)
        context = make_context()
        context.args = ["10000", WALLET]

        run(swap_command(update, context))

        assert "您已成功提交兑换请求" in update.message.reply_text.texts[0]
        assert total(app_database) == 2000

    def test_the_command_does_not_charge_when_the_balance_is_too_low(self, app_database):
        seed_user(app_database, 1, free=500)
        update = make_command_update(user_id=1, chat_id=9, message_id=11)
        context = make_context()
        context.args = ["10000", WALLET]

        run(swap_command(update, context))

        assert "您的金币不足" in update.message.reply_text.texts[0]
        assert self.requests(app_database) == []
        assert ledger_rows(app_database) == []


class TestKindnessGift:
    def gifts(self, url):
        return fetch(url, "SELECT recipient_id, amount FROM kindness_gifts ORDER BY id")

    def test_a_first_gift_is_credited_and_recorded_together(self, app_database):
        seed_user(app_database, 1, free=3, name="alice")

        outcome = run(user_tools.grant_kindness(1, 7))

        assert (outcome.granted, outcome.coins_before, outcome.amount) == (True, 3, 7)
        assert outcome.recipient_name == "alice"
        assert total(app_database) == 10
        assert self.gifts(app_database) == [{"recipient_id": 1, "amount": 7}]
        assert ledger_keys(app_database) == ["kindness:1:never"]

    def test_a_second_gift_inside_the_cooldown_is_refused(self, app_database):
        seed_user(app_database, 1, free=3)
        run(user_tools.grant_kindness(1, 7))

        again = run(user_tools.grant_kindness(1, 5))

        assert again.granted is False
        assert again.last_amount == 7
        assert total(app_database) == 10
        assert len(self.gifts(app_database)) == 1

    def test_the_next_window_opens_after_24_hours_with_a_new_identity(self, app_database):
        seed_user(app_database, 1, free=3)
        run(user_tools.grant_kindness(1, 7))
        execute(
            app_database,
            "UPDATE kindness_gifts SET created_at = created_at - INTERVAL 25 HOUR",
        )
        old_stamp = fetch_scalar(app_database, "SELECT created_at FROM kindness_gifts")

        outcome = run(user_tools.grant_kindness(1, 4))

        assert outcome.granted is True
        assert total(app_database) == 3 + 7 + 4
        assert ledger_keys(app_database) == [
            "kindness:1:never",
            user_tools.kindness_op_key(1, old_stamp),
        ]

    def test_concurrent_gifts_in_one_window_credit_once(self, app_database):
        seed_user(app_database, 1, free=0)

        async def scenario():
            return await gather_all(*[user_tools.grant_kindness(1, 6) for _ in range(6)])

        results = run(scenario())

        assert all(not isinstance(item, Exception) for item in results), results
        assert [item.granted for item in results].count(True) == 1
        assert total(app_database) == 6
        assert len(self.gifts(app_database)) == 1

    def test_an_unknown_recipient_gets_nothing(self, app_database):
        assert run(user_tools.grant_kindness(404, 5)) is None
        assert ledger_rows(app_database) == []

    def test_a_failure_after_the_credit_leaves_no_gift_record(self, app_database, monkeypatch):
        seed_user(app_database, 1, free=0)
        real_credit = fail_after(monkeypatch, "credit")

        with pytest.raises(RuntimeError):
            run(user_tools.grant_kindness(1, 6))
        monkeypatch.setattr(balance, "credit", real_credit)

        assert total(app_database) == 0
        assert self.gifts(app_database) == []
        # 没有留下记录：冷却没有被消耗，重试可以赠币。
        assert run(user_tools.grant_kindness(1, 6)).granted is True
        assert total(app_database) == 6

    def test_a_cleared_gift_record_does_not_allow_a_second_credit_in_the_same_window(
        self, app_database
    ):
        seed_user(app_database, 1, free=0)
        run(user_tools.grant_kindness(1, 6))
        # 只剩账本里的 kindness:1:never：同一个窗口不会再入账。
        execute(app_database, "DELETE FROM kindness_gifts")

        again = run(user_tools.grant_kindness(1, 6))

        assert again.granted is False
        assert again.last_amount == 6
        assert total(app_database) == 6
        assert self.gifts(app_database) == []

    def test_the_tool_grants_once_and_then_reports_the_cooldown(self, app_database):
        seed_user(app_database, 1, free=3, name="alice")

        async def scenario():
            db.set_main_loop(asyncio.get_running_loop())
            tool_context.set_tool_request_context({"user_id": 1})
            # 工具是同步函数，在工作线程里通过 run_sync 回到主事件循环。
            first = await asyncio.to_thread(user_tools.kindness_gift_tool, amount=5)
            second = await asyncio.to_thread(user_tools.kindness_gift_tool, amount=5)
            return first, second

        first, second = run(scenario())

        assert first["status"] == "granted"
        assert (first["amount"], first["recipient_coins_before"], first["recipient_coins_after"]) == (
            5,
            3,
            8,
        )
        assert first["recipient_username"] == "@alice"
        assert second["status"] == "cooldown"
        assert second["last_amount"] == 5
        assert total(app_database) == 8

    def test_the_tool_clamps_the_amount_and_reports_unknown_recipients(self, app_database):
        seed_user(app_database, 1, free=0)

        async def scenario():
            db.set_main_loop(asyncio.get_running_loop())
            tool_context.set_tool_request_context({"user_id": 1})
            granted = await asyncio.to_thread(user_tools.kindness_gift_tool, amount=999)
            tool_context.set_tool_request_context({"user_id": 404})
            missing = await asyncio.to_thread(user_tools.kindness_gift_tool, amount=5)
            return granted, missing

        granted, missing = run(scenario())

        assert granted["amount"] == 10
        assert missing == {"error": "Recipient user not found"}


class TestBribe:
    @pytest.fixture(autouse=True)
    def fixed_affection_gain(self, monkeypatch):
        monkeypatch.setattr(bribe.random, "randint", lambda low, high: 5)

    def affection(self, url, user_id=1):
        return fetch_scalar(
            url, "SELECT affection FROM ai_user_affection WHERE user_id = %s", (user_id,)
        ) or 0

    def pay(self, coins, message_id=11, before=0):
        return bribe._pay_bribe(1, coins, before, bribe.bribe_op_key(9, message_id))

    def test_the_debit_and_the_affection_gain_are_committed_together(self, app_database):
        seed_user(app_database, 1, free=500)

        outcome = run(self.pay(200))

        assert (outcome.status, outcome.total_gain, outcome.affection_after) == (
            BribeStatus.PAID,
            10,
            10,
        )
        assert total(app_database) == 300
        assert self.affection(app_database) == 10
        assert ledger_keys(app_database) == ["bribe:9:11"]

    def test_an_insufficient_balance_changes_nothing(self, app_database):
        seed_user(app_database, 1, free=150)

        outcome = run(self.pay(200))

        assert (outcome.status, outcome.balance_total) == (BribeStatus.INSUFFICIENT, 150)
        assert total(app_database) == 150
        assert self.affection(app_database) == 0
        assert ledger_rows(app_database) == []

    def test_the_same_command_delivered_twice_is_processed_once(self, app_database):
        seed_user(app_database, 1, free=500)

        run(self.pay(200))
        replay = run(self.pay(200))

        assert replay.status is BribeStatus.REPLAYED
        assert total(app_database) == 300
        assert self.affection(app_database) == 10

    def test_a_failure_while_raising_the_affection_returns_the_coins(
        self, app_database, monkeypatch
    ):
        seed_user(app_database, 1, free=500)

        async def failing_update(*args, **kwargs):
            raise RuntimeError("好感度写入失败")

        monkeypatch.setattr(bribe.process_user, "update_user_affection", failing_update)
        with pytest.raises(RuntimeError):
            run(self.pay(200))

        assert total(app_database) == 500
        assert self.affection(app_database) == 0
        assert ledger_rows(app_database) == []

    def test_the_affection_stops_at_the_cap(self, app_database):
        seed_user(app_database, 1, free=3000)
        execute(
            app_database,
            "INSERT INTO ai_user_affection (user_id, affection) VALUES (1, 90)",
        )

        outcome = run(self.pay(2000, before=90))  # 20 次 +5，从 90 起只能涨到 100

        assert outcome.affection_after == 100
        assert outcome.total_gain == 10

    def test_an_unregistered_user_is_reported(self, app_database):
        outcome = run(
            bribe._pay_bribe(404, 100, 0, bribe.bribe_op_key(9, 1))
        )

        assert outcome.status is BribeStatus.NOT_REGISTERED


class TestAffectionUpdate:
    def test_a_single_change_is_limited_to_ten_points(self, app_database):
        seed_user(app_database, 1)

        assert run(process_user.update_user_affection(1, 15)) == 10
        assert run(process_user.update_user_affection(1, -25)) == 0

    def test_inside_a_caller_transaction_the_change_rolls_back_with_it(self, app_database):
        seed_user(app_database, 1)

        async def scenario():
            async with mysql_connection.transaction() as connection:
                await process_user.update_user_affection(1, 5, connection=connection)
                raise RuntimeError("事务失败")

        with pytest.raises(RuntimeError):
            run(scenario())

        assert fetch_scalar(app_database, "SELECT COUNT(*) FROM ai_user_affection") == 0
