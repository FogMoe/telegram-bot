"""加密货币入口的 repository：BTC 预测记录与 $FOGMOE 兑换请求的语句级语义（真实 MySQL）。

预测与兑换的业务规则（扣款、过期退款、并发）在 test_btc_prediction.py 与 test_swap_kindness_bribe.py。
"""

from datetime import datetime, timedelta

import pytest
from economy_support import seed_user
from mysql_support import execute, fetch_scalar, run

from fogmoe_telegram_bot.core import sql
from fogmoe_telegram_bot.features.crypto.repositories import predictions as predictions_repository
from fogmoe_telegram_bot.features.crypto.repositories import swaps as swaps_repository

NOW = datetime(2026, 10, 5, 8, 30, 0)


def in_transaction(work):
    async def scenario():
        async with sql.transaction() as connection:
            return await work(connection)

    return run(scenario())


def predict(user_id=1, *, kind="up", amount=40, start=NOW, minutes=10):
    return in_transaction(
        lambda c: predictions_repository.replace_prediction(
            c,
            user_id,
            predict_type=kind,
            amount=amount,
            start_price=65000.5,
            start_time=start,
            end_time=start + timedelta(minutes=minutes),
        )
    )


class TestPredictionsRepository:
    def test_a_prediction_round_trips_with_typed_fields(self, app_database):
        seed_user(app_database, 1)

        predict()

        record = run(predictions_repository.get_unsettled(1))
        assert record == predictions_repository.Prediction(
            "up", 40, 65000.5, NOW, NOW + timedelta(minutes=10)
        )

    def test_the_running_prediction_ends_when_its_time_is_up(self, app_database):
        seed_user(app_database, 1)
        predict()

        before_end = NOW + timedelta(minutes=5)
        after_end = NOW + timedelta(minutes=11)

        assert run(predictions_repository.get_running(1, before_end)) is not None
        assert run(predictions_repository.get_running(1, after_end)) is None
        # 过了结束时间但还没结算的预测仍然是「未结算」，由下一次下注或结算处理。
        assert run(predictions_repository.get_unsettled(1)) is not None

    def test_users_without_a_prediction_have_none(self, app_database):
        assert run(predictions_repository.get_unsettled(404)) is None
        assert run(predictions_repository.get_running(404, NOW)) is None

    def test_a_new_prediction_replaces_the_settled_one(self, app_database):
        seed_user(app_database, 1)
        predict(kind="up", start=NOW)
        in_transaction(lambda c: predictions_repository.mark_completed(c, 1))
        assert run(predictions_repository.get_unsettled(1)) is None

        predict(kind="down", start=NOW + timedelta(hours=1))

        assert run(predictions_repository.get_unsettled(1)).predict_type == "down"
        assert fetch_scalar(app_database, "SELECT COUNT(*) FROM user_btc_predictions") == 1

    def test_unsettled_is_matched_by_the_start_time(self, app_database):
        seed_user(app_database, 1)
        predict()

        async def work(connection):
            return (
                await predictions_repository.is_unsettled(connection, 1, NOW),
                await predictions_repository.is_unsettled(connection, 1, NOW + timedelta(seconds=1)),
            )

        assert in_transaction(work) == (True, False)

    def test_completing_only_touches_unsettled_predictions(self, app_database):
        seed_user(app_database, 1)
        predict()

        in_transaction(lambda c: predictions_repository.complete_unsettled(c, 1))
        in_transaction(lambda c: predictions_repository.complete_unsettled(c, 1))

        assert run(predictions_repository.get_unsettled(1)) is None
        assert fetch_scalar(app_database, "SELECT is_completed FROM user_btc_predictions") == 1

    def test_writes_roll_back_with_the_callers_transaction(self, app_database):
        seed_user(app_database, 1)

        async def work(connection):
            await predictions_repository.replace_prediction(
                connection,
                1,
                predict_type="up",
                amount=40,
                start_price=1.0,
                start_time=NOW,
                end_time=NOW,
            )
            raise RuntimeError("injected failure")

        with pytest.raises(RuntimeError):
            in_transaction(work)

        assert fetch_scalar(app_database, "SELECT COUNT(*) FROM user_btc_predictions") == 0


class TestSwapsRepository:
    WALLET = "5iz3epFDf9SKvLNHWQ42f4wMMrENaudE9eMkxfBLFd2n"

    def insert(self, user_id=1, amount=10000):
        in_transaction(
            lambda c: swaps_repository.insert_swap_request(c, user_id, "someone", self.WALLET, amount)
        )

    def test_a_new_request_is_pending_with_its_details(self, app_database):
        seed_user(app_database, 1)
        assert run(swaps_repository.get_pending_swap(1)) is None

        self.insert()

        pending = run(swaps_repository.get_pending_swap(1))
        assert (pending.amount, pending.wallet_address) == (10000, self.WALLET)
        assert isinstance(pending.request_time, datetime)

    def test_the_latest_pending_request_is_returned(self, app_database):
        seed_user(app_database, 1)
        self.insert(amount=10000)
        self.insert(amount=20000)
        execute(
            app_database,
            "UPDATE token_swap_requests SET request_time = '2026-10-01 10:00:00' WHERE amount = 10000",
            "UPDATE token_swap_requests SET request_time = '2026-10-02 10:00:00' WHERE amount = 20000",
        )

        assert run(swaps_repository.get_pending_swap(1)).amount == 20000

    def test_processed_requests_are_not_pending(self, app_database):
        seed_user(app_database, 1)
        self.insert()
        execute(app_database, "UPDATE token_swap_requests SET status = 'completed'")

        assert run(swaps_repository.get_pending_swap(1)) is None

    def test_requests_are_per_user(self, app_database):
        seed_user(app_database, 1)
        seed_user(app_database, 2)
        self.insert(user_id=1)

        assert run(swaps_repository.get_pending_swap(2)) is None

    def test_reads_see_uncommitted_rows_of_the_callers_transaction(self, app_database):
        seed_user(app_database, 1)

        async def work(connection):
            await swaps_repository.insert_swap_request(connection, 1, "someone", self.WALLET, 10000)
            inside = await swaps_repository.get_pending_swap(1, connection=connection)
            outside = await swaps_repository.get_pending_swap(1)
            return inside is not None, outside is not None

        assert in_transaction(work) == (True, False)
