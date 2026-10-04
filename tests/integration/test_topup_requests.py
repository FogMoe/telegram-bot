"""管理员人工充值与卡密兑换：请求有持久身份，pending 只能转换一次，入账走余额服务。"""

import re
from datetime import datetime

import pytest
from economy_support import (
    Recorder,
    gather_all,
    ledger_rows,
    make_callback_update,
    make_context,
    seed_user,
    user_state,
)
from mysql_support import execute, fetch, fetch_scalar, run

from core import balance, config
from features.economy import charge_coin

ADMIN_ID = 9000


@pytest.fixture(autouse=True)
def _admin(monkeypatch):
    monkeypatch.setattr(charge_coin, "ADMIN_USER_ID", ADMIN_ID)
    monkeypatch.setattr(config, "ADMIN_USER_ID", ADMIN_ID)


def request_row(url, request_id):
    return fetch(url, "SELECT * FROM topup_requests WHERE id = %s", (request_id,))[0]


def new_request(url, user_id=1, coins=50, cents=199):
    return run(charge_coin.create_topup_request(user_id, coins, cents))


class TestDecideTopupRequest:
    def test_approve_credits_paid_coins_once_under_the_request_op_key(self, app_database):
        seed_user(app_database, 1, free=2)
        request_id = new_request(app_database)

        decision = run(charge_coin.decide_topup_request(request_id, "approve", ADMIN_ID))

        assert decision.outcome == "applied"
        assert decision.credit is not None and decision.credit.applied is True
        assert user_state(app_database, 1) == {"free": 2, "paid": 50, "plan": "paid"}
        row = request_row(app_database, request_id)
        assert row["status"] == "approved"
        assert row["decided_by"] == ADMIN_ID and row["decided_at"] is not None
        rows = ledger_rows(app_database, 1)
        assert [(r["op_key"], r["kind"], r["delta_paid"]) for r in rows] == [
            (f"topup:{request_id}", "credit", 50)
        ]

    def test_two_concurrent_approvals_credit_the_request_once(self, app_database):
        seed_user(app_database, 1)
        request_id = new_request(app_database)

        async def scenario():
            return await gather_all(
                charge_coin.decide_topup_request(request_id, "approve", ADMIN_ID),
                charge_coin.decide_topup_request(request_id, "approve", ADMIN_ID),
                charge_coin.decide_topup_request(request_id, "approve", ADMIN_ID),
            )

        results = run(scenario())

        assert all(not isinstance(item, Exception) for item in results), results
        outcomes = sorted(item.outcome for item in results)
        assert outcomes == ["already_decided", "already_decided", "applied"]
        assert user_state(app_database, 1)["paid"] == 50
        assert len(ledger_rows(app_database)) == 1
        assert request_row(app_database, request_id)["status"] == "approved"

    def test_a_decided_request_cannot_be_decided_again(self, app_database):
        seed_user(app_database, 1)
        request_id = new_request(app_database)
        run(charge_coin.decide_topup_request(request_id, "reject", ADMIN_ID))

        again = run(charge_coin.decide_topup_request(request_id, "approve", ADMIN_ID))

        assert again.outcome == "already_decided"
        assert again.request is not None and again.request.status == "rejected"
        assert user_state(app_database, 1)["paid"] == 0
        assert ledger_rows(app_database) == []
        assert request_row(app_database, request_id)["status"] == "rejected"

    def test_reject_changes_the_status_without_touching_the_balance(self, app_database):
        seed_user(app_database, 1, free=5)
        request_id = new_request(app_database)

        decision = run(charge_coin.decide_topup_request(request_id, "reject", ADMIN_ID))

        assert decision.outcome == "applied"
        assert user_state(app_database, 1)["free"] == 5
        assert ledger_rows(app_database) == []
        assert request_row(app_database, request_id)["status"] == "rejected"

    def test_block_sets_the_recharge_block_in_the_same_transaction(self, app_database):
        seed_user(app_database, 1)
        request_id = new_request(app_database)
        now = datetime(2026, 10, 5, 12, 0, 0)

        decision = run(charge_coin.decide_topup_request(request_id, "block", ADMIN_ID, now=now))

        assert decision.outcome == "applied"
        assert decision.blocked_until == datetime(2026, 10, 6, 12, 0, 0)
        assert fetch_scalar(
            app_database, "SELECT recharge_blocked_until FROM `user` WHERE id = 1"
        ) == datetime(2026, 10, 6, 12, 0, 0)
        assert request_row(app_database, request_id)["status"] == "blocked"
        assert user_state(app_database, 1)["paid"] == 0

    def test_a_failure_while_crediting_leaves_the_request_pending_and_retryable(
        self, app_database, monkeypatch
    ):
        seed_user(app_database, 1)
        request_id = new_request(app_database)
        real_credit = balance.credit

        async def failing_credit(*args, **kwargs):
            raise RuntimeError("入账失败")

        monkeypatch.setattr(balance, "credit", failing_credit)
        with pytest.raises(RuntimeError):
            run(charge_coin.decide_topup_request(request_id, "approve", ADMIN_ID))

        assert request_row(app_database, request_id)["status"] == "pending"
        assert user_state(app_database, 1)["paid"] == 0
        assert ledger_rows(app_database) == []

        monkeypatch.setattr(balance, "credit", real_credit)
        retry = run(charge_coin.decide_topup_request(request_id, "approve", ADMIN_ID))

        assert retry.outcome == "applied"
        assert user_state(app_database, 1)["paid"] == 50
        assert len(ledger_rows(app_database)) == 1

    def test_approving_for_a_user_that_no_longer_exists_keeps_the_request_pending(
        self, app_database
    ):
        request_id = new_request(app_database, user_id=404)

        decision = run(charge_coin.decide_topup_request(request_id, "approve", ADMIN_ID))

        assert decision.outcome == "user_missing"
        assert request_row(app_database, request_id)["status"] == "pending"
        assert ledger_rows(app_database) == []

    def test_unknown_request_is_reported(self, app_database):
        decision = run(charge_coin.decide_topup_request(12345, "approve", ADMIN_ID))

        assert decision.outcome == "not_found"


class TestAdminButtons:
    def test_user_request_creates_a_pending_row_and_buttons_carry_only_its_id(self, app_database):
        seed_user(app_database, 1)
        send = Recorder()
        update, _, edit = make_callback_update(from_user_id=1, data="topup_req_199_50")

        run(charge_coin.topup_request_callback(update, make_context(send_message=send)))

        rows = fetch(app_database, "SELECT id, user_id, coins, price_cents, status FROM topup_requests")
        assert len(rows) == 1
        assert (rows[0]["user_id"], rows[0]["coins"], rows[0]["price_cents"]) == (1, 50, 199)
        assert rows[0]["status"] == "pending"
        keyboard = send.calls[0][1]["reply_markup"]
        callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
        request_id = rows[0]["id"]
        assert callbacks == [
            f"topup_admin_approve_{request_id}",
            f"topup_admin_reject_{request_id}",
            f"topup_admin_block_{request_id}",
        ]
        for data in callbacks:  # 按钮里没有用户、金币数、价格
            assert re.fullmatch(r"topup_admin_(approve|reject|block)_\d+", data)
        assert "已通知管理员" in edit.texts[-1]

    def test_a_request_that_could_not_reach_the_admin_is_withdrawn(self, app_database):
        seed_user(app_database, 1)
        send = Recorder(fail_times=1)
        update, _, edit = make_callback_update(from_user_id=1, data="topup_req_199_50")

        run(charge_coin.topup_request_callback(update, make_context(send_message=send)))

        assert fetch_scalar(app_database, "SELECT COUNT(*) FROM topup_requests") == 0
        assert "联系管理员失败" in edit.texts[-1]

    def test_admin_approval_credits_and_a_second_click_reports_the_current_status(
        self, app_database
    ):
        seed_user(app_database, 1, name="alice")
        request_id = new_request(app_database)
        send = Recorder()
        data = f"topup_admin_approve_{request_id}"

        first, _, first_edit = make_callback_update(from_user_id=ADMIN_ID, data=data)
        run(charge_coin.topup_admin_callback(first, make_context(send_message=send)))
        second, _, second_edit = make_callback_update(from_user_id=ADMIN_ID, data=data)
        run(charge_coin.topup_admin_callback(second, make_context(send_message=send)))

        assert "已发放充值" in first_edit.texts[-1]
        assert "已处理" in second_edit.texts[-1] and "已发放" in second_edit.texts[-1]
        assert user_state(app_database, 1)["paid"] == 50
        assert len(ledger_rows(app_database)) == 1
        assert len(send.calls) == 1  # 只通知了用户一次

    def test_two_admin_clicks_at_once_credit_only_once(self, app_database):
        seed_user(app_database, 1)
        request_id = new_request(app_database)
        data = f"topup_admin_approve_{request_id}"

        async def scenario():
            updates = [
                make_callback_update(from_user_id=ADMIN_ID, data=data)[0] for _ in range(2)
            ]
            return await gather_all(
                *[
                    charge_coin.topup_admin_callback(update, make_context())
                    for update in updates
                ]
            )

        results = run(scenario())

        assert all(not isinstance(item, Exception) for item in results), results
        assert user_state(app_database, 1)["paid"] == 50

    @pytest.mark.parametrize(
        "legacy_data",
        [
            "topup_admin_approve_1_50_199",
            "topup_admin_reject_1_50_199",
            "topup_admin_block_1_50_199",
        ],
    )
    def test_legacy_buttons_are_refused_and_never_credit(self, app_database, legacy_data):
        seed_user(app_database, 1)
        update, _, edit = make_callback_update(from_user_id=ADMIN_ID, data=legacy_data)

        run(charge_coin.topup_admin_callback(update, make_context()))

        assert "已失效" in edit.texts[-1] and "重新发起" in edit.texts[-1]
        assert user_state(app_database, 1) == {"free": 0, "paid": 0, "plan": "free"}
        assert ledger_rows(app_database) == []
        assert fetch_scalar(app_database, "SELECT COUNT(*) FROM topup_requests") == 0

    def test_non_admin_users_cannot_decide_requests(self, app_database):
        seed_user(app_database, 1)
        request_id = new_request(app_database)
        update, answer, edit = make_callback_update(
            from_user_id=1, data=f"topup_admin_approve_{request_id}"
        )

        run(charge_coin.topup_admin_callback(update, make_context()))

        assert answer.calls and answer.calls[0][1].get("show_alert") is True
        assert edit.calls == []
        assert request_row(app_database, request_id)["status"] == "pending"
        assert user_state(app_database, 1)["paid"] == 0

    @pytest.mark.parametrize("bad_data", ["topup_admin_approve", "topup_admin_approve_x", "topup_admin_nuke_5"])
    def test_malformed_buttons_are_refused(self, app_database, bad_data):
        update, _, edit = make_callback_update(from_user_id=ADMIN_ID, data=bad_data)

        run(charge_coin.topup_admin_callback(update, make_context()))

        assert "无效" in edit.texts[-1]
        assert ledger_rows(app_database) == []

    def test_unknown_request_id_is_reported(self, app_database):
        update, _, edit = make_callback_update(from_user_id=ADMIN_ID, data="topup_admin_approve_99")

        run(charge_coin.topup_admin_callback(update, make_context()))

        assert "不存在" in edit.texts[-1]
        assert ledger_rows(app_database) == []


class TestRedemptionCodes:
    CODE = "123e4567-e89b-12d3-a456-426614174000"

    def seed_code(self, url, amount=100):
        execute(url, ("INSERT INTO redemption_codes (code, amount) VALUES (%s, %s)", (self.CODE, amount)))
        return fetch_scalar(url, "SELECT id FROM redemption_codes WHERE code = %s", (self.CODE,))

    def test_redeeming_credits_paid_coins_under_the_code_row_op_key(self, app_database):
        seed_user(app_database, 1)
        code_id = self.seed_code(app_database)

        success, result = run(charge_coin.verify_and_use_code(1, self.CODE))

        assert (success, result) == (True, 100)
        assert user_state(app_database, 1) == {"free": 0, "paid": 100, "plan": "paid"}
        assert [r["op_key"] for r in ledger_rows(app_database)] == [f"redeem:{code_id}"]
        used = fetch(app_database, "SELECT is_used, used_by FROM redemption_codes")[0]
        assert used["is_used"] == 1 and used["used_by"] == 1

    def test_a_code_cannot_be_redeemed_twice(self, app_database):
        seed_user(app_database, 1)
        seed_user(app_database, 2)
        self.seed_code(app_database)
        run(charge_coin.verify_and_use_code(1, self.CODE))

        again = run(charge_coin.verify_and_use_code(2, self.CODE))

        assert again[0] is False
        assert user_state(app_database, 2)["paid"] == 0
        assert len(ledger_rows(app_database)) == 1

    def test_a_credit_failure_leaves_the_code_unused(self, app_database, monkeypatch):
        seed_user(app_database, 1)
        self.seed_code(app_database)
        real_credit = balance.credit

        async def failing_credit(*args, **kwargs):
            raise RuntimeError("入账失败")

        monkeypatch.setattr(balance, "credit", failing_credit)
        success, _ = run(charge_coin.verify_and_use_code(1, self.CODE))

        assert success is False
        assert fetch_scalar(app_database, "SELECT is_used FROM redemption_codes") == 0
        assert user_state(app_database, 1)["paid"] == 0

        monkeypatch.setattr(balance, "credit", real_credit)
        assert run(charge_coin.verify_and_use_code(1, self.CODE)) == (True, 100)
        assert user_state(app_database, 1)["paid"] == 100

    def test_unregistered_users_get_a_clear_message_and_the_code_stays_unused(self, app_database):
        self.seed_code(app_database)

        success, message = run(charge_coin.verify_and_use_code(404, self.CODE))

        assert success is False and "/me" in message
        assert fetch_scalar(app_database, "SELECT is_used FROM redemption_codes") == 0
        assert ledger_rows(app_database) == []
