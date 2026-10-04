"""余额服务的纯逻辑：op_key 派生与校验、扣款拆分、套餐规则、死锁重试、各业务的身份派生。

需要真实 MySQL 的行为（幂等、并发、退款）在 tests/integration/test_balance_service.py。
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from core import balance, chat_records, config, process_user, sql, stake_reward_pool
from features.conversation import billing
from features.economy import charge_coin, checkin


class TestOpKeys:
    def test_parts_are_joined_with_colons(self):
        assert balance.make_op_key("checkin", 123, date(2026, 10, 5)) == "checkin:123:2026-10-05"
        assert balance.make_op_key("chat", -1001234567890, 42) == "chat:-1001234567890:42"

    def test_one_off_keys_are_unique_and_valid(self):
        first, second = balance.new_op_key("legacy:x"), balance.new_op_key("legacy:x")

        assert first != second
        assert balance.check_op_key(first) == first

    def test_refund_keys_derive_from_the_original(self):
        assert balance.refund_op_key("pic:1:2") == "refund:pic:1:2"

    @pytest.mark.parametrize(
        "bad",
        ["", " ", "a b", "tab\there", "line\nbreak", "中文", "é", "x" * 129, "refund:pic:1:2"],
    )
    def test_invalid_keys_are_rejected(self, bad):
        with pytest.raises(balance.InvalidBalanceRequest):
            balance.check_op_key(bad)

    def test_the_longest_allowed_key_still_fits_a_derived_key_in_the_column(self):
        key = "k" * balance.OP_KEY_MAX_LENGTH

        assert balance.check_op_key(key) == key
        assert len(balance.refund_op_key(key)) <= balance.DERIVED_OP_KEY_MAX_LENGTH
        assert len(stake_reward_pool.pool_op_key(key)) <= balance.DERIVED_OP_KEY_MAX_LENGTH

    @pytest.mark.parametrize("amount", [0, -1, True, False, 1.5, "3", None, 2**31])
    def test_invalid_amounts_are_rejected(self, amount):
        with pytest.raises(balance.InvalidBalanceRequest):
            balance.check_amount(amount)

    def test_valid_amounts_pass_through(self):
        assert balance.check_amount(1) == 1
        assert balance.check_amount(balance.MAX_AMOUNT) == balance.MAX_AMOUNT

    def test_reason_and_ref_limits(self):
        assert balance.check_reason("ai_chat") == "ai_chat"
        with pytest.raises(balance.InvalidBalanceRequest):
            balance.check_reason("")
        with pytest.raises(balance.InvalidBalanceRequest):
            balance.check_reason("r" * 65)
        assert balance.check_ref(None) is None
        with pytest.raises(balance.InvalidBalanceRequest):
            balance.check_ref("has space")


class TestSplitDebit:
    @pytest.mark.parametrize(
        ("free", "paid", "amount", "expected"),
        [
            (10, 10, 4, (-4, 0)),
            (3, 10, 3, (-3, 0)),
            (3, 10, 5, (-3, -2)),
            (0, 10, 5, (0, -5)),
            (2, 3, 5, (-2, -3)),
        ],
    )
    def test_free_coins_go_first(self, free, paid, amount, expected):
        assert balance.split_debit(balance.UserBalances(free, paid), amount) == expected


class TestUserPlan:
    def test_paid_balance_means_paid_plan(self):
        assert balance.resolve_user_plan(1, 0) == "free"
        assert balance.resolve_user_plan(1, 1) == "paid"

    def test_the_administrator_is_always_admin(self, monkeypatch):
        monkeypatch.setattr(config, "ADMIN_USER_ID", 99)

        assert balance.resolve_user_plan(99, 0) == "admin"
        assert balance.resolve_user_plan(99, 50) == "admin"

    def test_process_user_exports_the_same_rule(self):
        assert process_user.resolve_user_plan is balance.resolve_user_plan
        assert process_user.USER_PLAN_PAID == "paid"

    def test_a_result_reports_the_plan_implied_by_its_balance(self):
        result = balance.BalanceResult(
            op_key="k",
            user_id=1,
            kind=balance.LedgerKind.DEBIT,
            applied=False,
            delta_free=-1,
            delta_paid=-2,
            balance_free=0,
            balance_paid=4,
            reason="test",
        )

        assert result.user_plan == "paid"
        assert result.amount == 3
        assert result.balance_total == 4


class _FakeDatabaseError(Exception):
    def __init__(self, code):
        super().__init__(code, "boom")
        self.orig = SimpleNamespace(args=(code, "boom"))


class TestDeadlockRetry:
    @pytest.fixture
    def transactions(self, monkeypatch):
        opened = []

        @asynccontextmanager
        async def fake_transaction():
            opened.append(object())
            yield opened[-1]

        monkeypatch.setattr(sql, "transaction", fake_transaction)
        monkeypatch.setattr(balance, "_DEADLOCK_BACKOFF_SECONDS", 0)
        return opened

    def test_a_deadlock_reruns_the_whole_transaction(self, transactions):
        attempts = []

        async def work(connection):
            attempts.append(connection)
            if len(attempts) == 1:
                raise _FakeDatabaseError(sql.MYSQL_ERROR_DEADLOCK)
            return "done"

        assert asyncio.run(balance.run_in_transaction(work)) == "done"
        assert len(transactions) == 2  # 每次重试都是一个新事务
        assert attempts[0] is not attempts[1]

    def test_other_errors_are_raised_immediately(self, transactions):
        async def work(connection):
            raise _FakeDatabaseError(1062)

        with pytest.raises(_FakeDatabaseError):
            asyncio.run(balance.run_in_transaction(work))
        assert len(transactions) == 1

    def test_a_persistent_deadlock_gives_up(self, transactions):
        async def work(connection):
            raise _FakeDatabaseError(sql.MYSQL_ERROR_DEADLOCK)

        with pytest.raises(_FakeDatabaseError):
            asyncio.run(balance.run_in_transaction(work))
        assert len(transactions) == balance._DEADLOCK_ATTEMPTS

    def test_domain_errors_are_not_retried(self, transactions):
        async def work(connection):
            raise balance.InsufficientBalance(1, 5, balance.UserBalances(0, 0))

        with pytest.raises(balance.InsufficientBalance):
            asyncio.run(balance.run_in_transaction(work))
        assert len(transactions) == 1


class TestMysqlErrorCodes:
    def test_codes_are_read_from_wrapped_and_raw_driver_errors(self):
        assert sql.mysql_error_code(_FakeDatabaseError(1213)) == 1213
        assert sql.is_deadlock_error(_FakeDatabaseError(1213))
        assert sql.is_duplicate_key_error(_FakeDatabaseError(1062))
        assert sql.mysql_error_code(RuntimeError("no code")) is None
        assert not sql.is_deadlock_error(_FakeDatabaseError(1062))


class TestFirstWriteRetry:
    @pytest.fixture(autouse=True)
    def _no_backoff(self, monkeypatch):
        monkeypatch.setattr(chat_records, "FIRST_WRITE_BACKOFF_SECONDS", 0)

    def test_deadlocks_and_duplicate_keys_are_retried_a_bounded_number_of_times(self):
        for code in (sql.MYSQL_ERROR_DEADLOCK, sql.MYSQL_ERROR_DUPLICATE_KEY):
            calls = []

            async def operation():
                calls.append(1)
                if len(calls) < 3:
                    raise _FakeDatabaseError(code)
                return "ok"

            assert asyncio.run(chat_records._retry_first_write(operation)) == "ok"
            assert len(calls) == 3

    def test_a_persistent_conflict_is_eventually_raised(self):
        calls = []

        async def operation():
            calls.append(1)
            raise _FakeDatabaseError(sql.MYSQL_ERROR_DEADLOCK)

        with pytest.raises(_FakeDatabaseError):
            asyncio.run(chat_records._retry_first_write(operation))
        assert len(calls) == chat_records.FIRST_WRITE_ATTEMPTS

    def test_unrelated_errors_are_not_retried(self):
        calls = []

        async def operation():
            calls.append(1)
            raise _FakeDatabaseError(1146)

        with pytest.raises(_FakeDatabaseError):
            asyncio.run(chat_records._retry_first_write(operation))
        assert calls == [1]


class TestBusinessIdentities:
    def test_checkin_key_is_per_user_per_day(self):
        assert checkin.checkin_op_key(7, date(2026, 10, 5)) == "checkin:7:2026-10-05"
        assert checkin.checkin_op_key(7, date(2026, 10, 6)) != checkin.checkin_op_key(
            7, date(2026, 10, 5)
        )

    def test_lottery_key_follows_the_previous_draw_time(self):
        first = process_user.lottery_op_key(7, None)
        second = process_user.lottery_op_key(7, datetime(2026, 10, 5, 8, 30, 1))

        assert first == "lottery:7:never"
        assert second == "lottery:7:20261005T083001"
        assert process_user.lottery_op_key(7, datetime(2026, 10, 5, 8, 30, 1)) == second
        assert process_user.lottery_op_key(8, datetime(2026, 10, 5, 8, 30, 1)) != second

    def test_lottery_rewards_stay_within_the_documented_tiers(self):
        draws = {process_user.draw_lottery_coins() for _ in range(2000)}

        assert draws <= set(range(1, 21))
        assert {1, 20} & draws  # 两端都能抽到：样本足够大

    def test_topup_op_key_and_buttons_only_carry_the_request_id(self):
        assert charge_coin.topup_op_key(17) == "topup:17"
        assert charge_coin.topup_admin_callback_data("approve", 17) == "topup_admin_approve_17"

    @pytest.mark.parametrize(
        ("data", "expected"),
        [
            ("topup_admin_approve_17", ("approve", 17)),
            ("topup_admin_reject_5", ("reject", 5)),
            ("topup_admin_block_123456789", ("block", 123456789)),
            ("topup_admin_approve_1_50_199", None),  # 旧格式
            ("topup_admin_approve_", None),
            ("topup_admin_approve_-1", None),
            ("topup_admin_delete_1", None),
            ("topup_req_199_50", None),
            ("", None),
        ],
    )
    def test_admin_button_parsing_is_strict(self, data, expected):
        assert charge_coin.parse_topup_admin_callback(data) == expected

    def test_legacy_buttons_are_recognised_as_such(self):
        assert charge_coin.is_legacy_topup_admin_callback("topup_admin_approve_1_50_199")
        assert charge_coin.is_legacy_topup_admin_callback("topup_admin_block_123_100_299")
        assert not charge_coin.is_legacy_topup_admin_callback("topup_admin_approve_17")
        assert not charge_coin.is_legacy_topup_admin_callback("garbage")

    def test_message_keys_cover_new_edited_and_anonymous_updates(self):
        assert billing.message_op_key(billing.TurnMessage(100, 10, 1)) == "chat:100:10"
        assert (
            billing.message_op_key(billing.TurnMessage(100, 10, 1, edit_stamp=1_700_000_000))
            == "chat:100:10:edit:1700000000"
        )
        assert (
            billing.message_op_key(billing.TurnMessage(100, None, 1, update_id=900))
            == "chat:100:update:900"
        )
        adhoc = billing.message_op_key(billing.TurnMessage(100, None, 1))
        assert adhoc.startswith("chat:adhoc:") and adhoc != billing.message_op_key(
            billing.TurnMessage(100, None, 1)
        )

    def test_turn_messages_read_the_edit_time_from_telegram_messages(self):
        edited = SimpleNamespace(
            message_id=10, edit_date=datetime.fromtimestamp(1_700_000_000, tz=None)
        )

        edited_turn = billing.TurnMessage.from_message(edited, chat_id=5, cost=2, edited=True)
        fresh_turn = billing.TurnMessage.from_message(edited, chat_id=5, cost=2, edited=False)

        assert edited_turn.edit_stamp == int(edited.edit_date.timestamp())
        assert fresh_turn.edit_stamp is None

    def test_pool_contributions_derive_their_key_from_the_spend(self):
        assert stake_reward_pool.pool_op_key("chat:1:2") == "pool:chat:1:2"
        assert stake_reward_pool.calculate_pool_add(5) == Decimal("1.00")
        assert stake_reward_pool.calculate_pool_add(1) == Decimal("0.20")
        assert stake_reward_pool.calculate_pool_add(0) == Decimal("0")
