"""BTC 价格预测：下注扣款与预测记录同事务，结算与过期退款只发生一次（真实 MySQL）。"""

from datetime import datetime, timedelta

import pytest
from economy_support import gather_all, ledger_keys, ledger_rows, seed_user, user_state
from mysql_support import execute, fetch, run

from core import balance
from features.crypto import crypto_predict

NOW = datetime.now().replace(microsecond=0)


def total(url, user_id=1):
    state = user_state(url, user_id)
    return state["free"] + state["paid"]


def prediction(url, user_id=1):
    rows = fetch(
        url,
        "SELECT predict_type, amount, start_price, start_time, end_time, is_completed "
        "FROM user_btc_predictions WHERE user_id = %s",
        (user_id,),
    )
    return rows[0] if rows else None


def seed_prediction(url, start, *, user_id=1, amount=20, predict_type="up", completed=False):
    execute(
        url,
        (
            "INSERT INTO user_btc_predictions "
            "(user_id, predict_type, amount, start_price, start_time, end_time, is_completed) "
            "VALUES (%s, %s, %s, 100, %s, %s, %s)",
            (user_id, predict_type, amount, start, start + timedelta(minutes=10), completed),
        ),
    )


@pytest.fixture
def price(monkeypatch):
    """可控的 BTC 价格。"""
    holder = {"value": 100.0, "error": None}

    async def fake_price():
        return holder["value"], holder["error"]

    monkeypatch.setattr(crypto_predict, "get_btc_price", fake_price)
    return holder


def fail_after_credit(monkeypatch):
    real = balance.credit

    async def wrapper(*args, **kwargs):
        await real(*args, **kwargs)
        raise RuntimeError("injected failure")

    monkeypatch.setattr(balance, "credit", wrapper)
    return real


class TestCreatePrediction:
    def test_a_bet_is_debited_and_recorded_together(self, app_database):
        seed_user(app_database, 1, free=100)

        ok, error = run(crypto_predict.create_prediction(1, "up", 40, 100.0))

        assert (ok, error) == (True, None)
        assert total(app_database) == 60
        row = prediction(app_database)
        assert (row["predict_type"], row["amount"], row["is_completed"]) == ("up", 40, 0)
        rows = ledger_rows(app_database)
        assert [(r["kind"], r["reason"]) for r in rows] == [("debit", "btc_bet")]
        # op_key 由预测的身份（用户 + 开始时间）派生。
        assert rows[0]["op_key"] == crypto_predict.prediction_op_key(1, row["start_time"], "bet")

    def test_an_insufficient_balance_leaves_no_prediction(self, app_database):
        seed_user(app_database, 1, free=10)

        ok, error = run(crypto_predict.create_prediction(1, "up", 40, 100.0))

        assert (ok, error) == (False, "金币不足")
        assert prediction(app_database) is None
        assert total(app_database) == 10
        assert ledger_rows(app_database) == []

    def test_an_unregistered_user_cannot_bet(self, app_database):
        ok, error = run(crypto_predict.create_prediction(404, "up", 40, 100.0))

        assert (ok, error) == (False, "金币不足")
        assert prediction(app_database, 404) is None

    def test_a_running_prediction_blocks_a_second_bet(self, app_database):
        seed_user(app_database, 1, free=100)
        run(crypto_predict.create_prediction(1, "up", 40, 100.0))

        ok, error = run(crypto_predict.create_prediction(1, "down", 40, 100.0))

        assert (ok, error) == (False, "您已经有一个正在进行的预测")
        assert total(app_database) == 60
        assert len(ledger_rows(app_database)) == 1

    def test_concurrent_bets_by_one_user_open_exactly_one_prediction(self, app_database):
        seed_user(app_database, 1, free=500)

        async def scenario():
            return await gather_all(
                *[crypto_predict.create_prediction(1, "up", 40, 100.0) for _ in range(5)]
            )

        results = run(scenario())

        assert all(not isinstance(item, Exception) for item in results), results
        assert [item[0] for item in results].count(True) == 1
        assert total(app_database) == 460
        assert len(ledger_rows(app_database)) == 1

    def test_a_failure_after_the_debit_leaves_no_prediction_and_no_charge(
        self, app_database, monkeypatch
    ):
        seed_user(app_database, 1, free=100)
        real = balance.debit

        async def debit_then_fail(*args, **kwargs):
            await real(*args, **kwargs)
            raise RuntimeError("injected failure")

        monkeypatch.setattr(balance, "debit", debit_then_fail)

        ok, error = run(crypto_predict.create_prediction(1, "up", 40, 100.0))

        assert ok is False and error.startswith("创建预测时出错")
        assert prediction(app_database) is None
        assert total(app_database) == 100
        assert ledger_rows(app_database) == []

    def test_an_expired_prediction_is_refunded_to_its_original_split_before_the_new_bet(
        self, app_database
    ):
        seed_user(app_database, 1, free=10, paid=30)
        stale_start = NOW - timedelta(minutes=30)
        seed_prediction(app_database, stale_start, amount=40)
        run(
            balance.debit_standalone(
                1,
                40,
                op_key=crypto_predict.prediction_op_key(1, stale_start, "bet"),
                reason="btc_bet",
            )
        )
        assert total(app_database) == 0

        ok, _ = run(crypto_predict.create_prediction(1, "down", 25, 100.0))

        assert ok is True
        assert total(app_database) == 40 - 25
        stale_key = crypto_predict.prediction_op_key(1, stale_start, "bet")
        assert ledger_keys(app_database)[:2] == [stale_key, f"refund:{stale_key}"]
        assert prediction(app_database)["predict_type"] == "down"
        # 原路退回：先扣的免费部分回到免费，付费部分回到付费。
        refund = ledger_rows(app_database)[1]
        assert (refund["delta_free"], refund["delta_paid"]) == (10, 30)

    def test_an_expired_prediction_from_before_the_ledger_is_refunded_once(self, app_database):
        seed_user(app_database, 1, free=0)
        stale_start = NOW - timedelta(minutes=30)
        seed_prediction(app_database, stale_start, amount=40)

        ok, error = run(crypto_predict.create_prediction(1, "up", 100, 100.0))

        # 退回 40 之后余额仍不够新的 100：退款已经提交，新预测没有创建。
        assert (ok, error) == (False, "金币不足")
        assert total(app_database) == 40
        assert ledger_keys(app_database) == [
            crypto_predict.prediction_op_key(1, stale_start, "expired")
        ]
        assert prediction(app_database)["is_completed"] == 1

        again = run(crypto_predict.create_prediction(1, "up", 100, 100.0))
        assert again == (False, "金币不足")
        assert total(app_database) == 40  # 不会再退一次


class TestPredictionResult:
    def create(self, url, amount=20, predict_type="up", free=100):
        seed_user(url, 1, free=free)
        run(crypto_predict.create_prediction(1, predict_type, amount, 100.0))
        return prediction(url)["start_time"]

    def test_a_correct_prediction_pays_one_point_eight_times_the_bet_once(
        self, app_database, price
    ):
        start = self.create(app_database)
        price["value"] = 120.0

        result = run(crypto_predict.check_prediction_result(1))

        assert result["is_correct"] is True and result["reward"] == 36
        assert total(app_database) == 100 - 20 + 36
        assert prediction(app_database)["is_completed"] == 1
        assert ledger_keys(app_database)[-1] == crypto_predict.prediction_op_key(1, start, "win")

        again = run(crypto_predict.check_prediction_result(1))
        assert again is None
        assert total(app_database) == 116

    def test_a_wrong_prediction_pays_nothing(self, app_database, price):
        self.create(app_database)
        price["value"] = 80.0

        result = run(crypto_predict.check_prediction_result(1))

        assert result["is_correct"] is False and result["reward"] == 0
        assert total(app_database) == 80
        assert len(ledger_rows(app_database)) == 1
        assert prediction(app_database)["is_completed"] == 1

    def test_concurrent_settlements_pay_once(self, app_database, price):
        self.create(app_database)
        price["value"] = 120.0

        async def scenario():
            return await gather_all(
                *[crypto_predict.check_prediction_result(1) for _ in range(5)]
            )

        results = run(scenario())

        assert all(not isinstance(item, Exception) for item in results), results
        assert sum(1 for item in results if item) == 1
        assert total(app_database) == 116

    def test_an_unavailable_price_leaves_the_prediction_pending(self, app_database, price):
        self.create(app_database)
        price["value"], price["error"] = None, "获取比特币价格失败"

        assert run(crypto_predict.check_prediction_result(1)) is None
        assert prediction(app_database)["is_completed"] == 0
        assert total(app_database) == 80

        price["value"], price["error"] = 120.0, None
        assert run(crypto_predict.check_prediction_result(1))["reward"] == 36

    def test_a_failure_after_the_payout_rolls_the_settlement_back(
        self, app_database, price, monkeypatch
    ):
        self.create(app_database)
        price["value"] = 120.0
        real_credit = fail_after_credit(monkeypatch)

        assert run(crypto_predict.check_prediction_result(1)) is None

        assert prediction(app_database)["is_completed"] == 0
        assert total(app_database) == 80
        monkeypatch.setattr(balance, "credit", real_credit)
        retry = run(crypto_predict.check_prediction_result(1))
        assert retry["reward"] == 36
        assert total(app_database) == 116
