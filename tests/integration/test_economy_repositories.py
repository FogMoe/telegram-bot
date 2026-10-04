"""经济领域的 repository：单条语句级别的读写语义，以及「事务由调用方持有」的约定（真实 MySQL）。

业务规则（余额、幂等、状态转换）在 operations 的集成测试里；这里只验证每个 repository 函数自己的行为。
"""

from datetime import date, datetime

import pytest
from economy_support import seed_user
from mysql_support import execute, fetch, fetch_scalar, run
from sqlalchemy.exc import IntegrityError

from core import sql, user_records
from features.economy.repositories import charge as charge_repository
from features.economy.repositories import checkin as checkin_repository
from features.economy.repositories import coins as coins_repository
from features.economy.repositories import invitations as invitations_repository
from features.economy.repositories import lottery as lottery_repository
from features.economy.repositories import shop as shop_repository
from features.economy.repositories import stake as stake_repository
from features.economy.repositories import tasks as tasks_repository
from features.economy.repositories import web_passwords as web_password_repository


def in_transaction(work):
    """在一个事务里运行 `work(connection)` 并返回结果；抛出异常时事务回滚。"""

    async def scenario():
        async with sql.transaction() as connection:
            return await work(connection)

    return run(scenario())


class TestUserRecords:
    def test_balances_permission_and_name_are_read_back(self, app_database):
        seed_user(app_database, 1, free=3, paid=4, name="alice")
        execute(app_database, "UPDATE `user` SET permission = 2 WHERE id = 1")

        assert run(user_records.get_coin_balances(1)) == (3, 4)
        assert run(user_records.get_permission(1)) == 2
        assert run(user_records.get_name(1)) == "alice"
        assert run(user_records.find_id_by_name("alice")) == 1
        assert run(user_records.check_user_exists(1)) is True

    def test_missing_users_have_neutral_values(self, app_database):
        assert run(user_records.get_coin_balances(404)) == (0, 0)
        assert run(user_records.get_permission(404)) == 0
        assert run(user_records.get_name(404)) is None
        assert run(user_records.find_id_by_name("nobody")) is None
        assert run(user_records.check_user_exists(404)) is False

    def test_reads_can_join_the_callers_transaction(self, app_database):
        async def work(connection):
            await connection.exec_driver_sql(
                "INSERT INTO user (id, name, coins) VALUES (5, 'bob', 0)"
            )
            # 同一个事务里读得到尚未提交的行，别的连接读不到。
            inside = await user_records.check_user_exists(5, connection=connection)
            outside = await user_records.check_user_exists(5)
            return inside, outside

        assert in_transaction(work) == (True, False)

    def test_create_user_opens_the_account_with_zero_coins(self, app_database):
        in_transaction(lambda connection: user_records.create_user(connection, 8, "carol"))

        assert run(user_records.get_coin_balances(8)) == (0, 0)
        assert run(user_records.get_name(8)) == "carol"

    def test_creating_an_existing_user_is_a_duplicate_key_error(self, app_database):
        seed_user(app_database, 8)

        with pytest.raises(IntegrityError) as caught:
            in_transaction(lambda connection: user_records.create_user(connection, 8, "again"))

        assert sql.is_duplicate_key_error(caught.value)


class TestShopRepository:
    def test_the_memory_limit_starts_at_the_default_and_grows_by_the_given_amount(
        self, app_database
    ):
        seed_user(app_database, 1)

        async def work(connection):
            before = await shop_repository.get_permanent_records_limit(connection, 1)
            await shop_repository.increase_permanent_records_limit(connection, 1, 3)
            after = await shop_repository.get_permanent_records_limit(connection, 1)
            return before, after

        assert in_transaction(work) == (100, 103)

    def test_an_unknown_user_has_no_limit(self, app_database):
        async def work(connection):
            return await shop_repository.get_permanent_records_limit(connection, 404)

        assert in_transaction(work) is None

    def test_the_permission_level_is_written(self, app_database):
        seed_user(app_database, 1)

        in_transaction(lambda connection: shop_repository.set_permission(connection, 1, 2))

        assert run(user_records.get_permission(1)) == 2

    def test_writes_roll_back_with_the_callers_transaction(self, app_database):
        seed_user(app_database, 1)

        async def work(connection):
            await shop_repository.set_permission(connection, 1, 3)
            raise RuntimeError("injected failure")

        with pytest.raises(RuntimeError):
            in_transaction(work)

        assert run(user_records.get_permission(1)) == 0


class TestStakeRepository:
    NOW = datetime(2026, 10, 5, 8, 30, 15)

    def test_a_stake_round_trips_with_a_typed_record(self, app_database):
        seed_user(app_database, 1)

        in_transaction(lambda c: stake_repository.insert_stake(c, 1, 500, self.NOW))

        record = run(stake_repository.get_stake(1))
        assert record == stake_repository.StakeRecord(500, self.NOW, None)

    def test_the_reward_window_and_deletion(self, app_database):
        seed_user(app_database, 1)
        in_transaction(lambda c: stake_repository.insert_stake(c, 1, 500, self.NOW))
        later = datetime(2026, 10, 12, 8, 30, 15)

        in_transaction(lambda c: stake_repository.set_last_reward_time(c, 1, later))
        assert run(stake_repository.get_stake(1)).last_reward_time == later

        in_transaction(lambda c: stake_repository.delete_stake(c, 1))
        assert run(stake_repository.get_stake(1)) is None

    def test_one_stake_per_user(self, app_database):
        seed_user(app_database, 1)
        in_transaction(lambda c: stake_repository.insert_stake(c, 1, 500, self.NOW))

        with pytest.raises(IntegrityError):
            in_transaction(lambda c: stake_repository.insert_stake(c, 1, 100, self.NOW))

    def test_totals_cover_free_and_paid_coins_and_all_stakes(self, app_database):
        seed_user(app_database, 1, free=10, paid=5)
        seed_user(app_database, 2, free=20)
        in_transaction(lambda c: stake_repository.insert_stake(c, 1, 7, self.NOW))
        in_transaction(lambda c: stake_repository.insert_stake(c, 2, 8, self.NOW))

        assert run(stake_repository.sum_user_coins()) == 35
        assert run(stake_repository.sum_staked()) == 15

    def test_totals_are_zero_when_there_is_nothing(self, app_database):
        assert run(stake_repository.sum_user_coins()) == 0
        assert run(stake_repository.sum_staked()) == 0

    def test_an_insert_rolls_back_with_the_callers_transaction(self, app_database):
        seed_user(app_database, 1)

        async def work(connection):
            await stake_repository.insert_stake(connection, 1, 500, self.NOW)
            raise RuntimeError("injected failure")

        with pytest.raises(RuntimeError):
            in_transaction(work)

        assert run(stake_repository.get_stake(1)) is None


class TestInvitationsRepository:
    def test_an_invitation_lists_the_referrer_and_the_invited(self, app_database):
        seed_user(app_database, 1, name="boss")
        seed_user(app_database, 10, name="first")
        seed_user(app_database, 11, name="second")
        in_transaction(lambda c: invitations_repository.insert_invitation(c, 10, 1))
        in_transaction(lambda c: invitations_repository.insert_invitation(c, 11, 1))
        execute(
            app_database,
            "UPDATE user_invitations SET invitation_time = '2026-10-01 10:00:00' WHERE invited_user_id = 10",
            "UPDATE user_invitations SET invitation_time = '2026-10-02 10:00:00' WHERE invited_user_id = 11",
        )

        assert run(invitations_repository.count_invited(1)) == 2
        invited = run(invitations_repository.list_invited(1))
        assert [(item.user_id, item.name) for item in invited] == [(11, "second"), (10, "first")]
        assert invited[0].invited_at == datetime(2026, 10, 2, 10, 0, 0)
        assert run(invitations_repository.get_referrer(10)) == invitations_repository.Referrer(
            1, "boss"
        )
        assert fetch_scalar(app_database, "SELECT reward_claimed FROM user_invitations LIMIT 1") == 1

    def test_users_without_invitations_have_neutral_values(self, app_database):
        seed_user(app_database, 1)

        assert run(invitations_repository.count_invited(1)) == 0
        assert run(invitations_repository.list_invited(1)) == []
        assert run(invitations_repository.get_referrer(1)) is None

    def test_an_invitee_can_only_be_invited_once(self, app_database):
        for user_id in (1, 2, 10):
            seed_user(app_database, user_id)
        in_transaction(lambda c: invitations_repository.insert_invitation(c, 10, 1))

        with pytest.raises(IntegrityError) as caught:
            in_transaction(lambda c: invitations_repository.insert_invitation(c, 10, 2))

        assert sql.is_duplicate_key_error(caught.value)


class TestTasksRepository:
    def test_completion_is_recorded_per_user_and_task(self, app_database):
        seed_user(app_database, 1)

        assert run(tasks_repository.is_completed(1, 1)) is False
        in_transaction(lambda c: tasks_repository.record_completion(c, 1, 1))

        assert run(tasks_repository.is_completed(1, 1)) is True
        assert run(tasks_repository.is_completed(1, 2)) is False

    def test_a_task_can_only_be_recorded_once(self, app_database):
        seed_user(app_database, 1)
        in_transaction(lambda c: tasks_repository.record_completion(c, 1, 1))

        with pytest.raises(IntegrityError):
            in_transaction(lambda c: tasks_repository.record_completion(c, 1, 1))


class TestCheckinRepository:
    def test_saving_twice_keeps_one_row_with_the_latest_values(self, app_database):
        seed_user(app_database, 1)
        assert run(checkin_repository.get_checkin(1)) is None

        in_transaction(lambda c: checkin_repository.save_checkin(c, 1, date(2026, 10, 4), 1))
        in_transaction(lambda c: checkin_repository.save_checkin(c, 1, date(2026, 10, 5), 2))

        assert run(checkin_repository.get_checkin(1)) == checkin_repository.CheckinRecord(
            date(2026, 10, 5), 2
        )
        assert fetch_scalar(app_database, "SELECT COUNT(*) FROM user_checkin") == 1


class TestCoinsRepository:
    DAY = date(2026, 10, 5)

    def test_the_daily_give_count_is_per_user_and_per_day(self, app_database):
        seed_user(app_database, 1)

        async def work(connection):
            start = await coins_repository.get_daily_give_count(connection, 1, self.DAY)
            await coins_repository.increment_daily_give_count(connection, 1, self.DAY)
            await coins_repository.increment_daily_give_count(connection, 1, self.DAY)
            today = await coins_repository.get_daily_give_count(connection, 1, self.DAY)
            tomorrow = await coins_repository.get_daily_give_count(
                connection, 1, date(2026, 10, 6)
            )
            return start, today, tomorrow

        assert in_transaction(work) == (0, 2, 0)

    def test_the_leaderboard_is_ordered_by_total_coins_and_limited(self, app_database):
        seed_user(app_database, 1, free=5, name="small")
        seed_user(app_database, 2, free=1, paid=50, name="big")
        seed_user(app_database, 3, free=20, name="medium")

        top_two = run(coins_repository.richest_users(2))

        assert top_two == [
            coins_repository.RichEntry("big", 51),
            coins_repository.RichEntry("medium", 20),
        ]
        assert [entry.name for entry in run(coins_repository.richest_users(10))] == [
            "big",
            "medium",
            "small",
        ]


class TestChargeRepository:
    CODE = "123e4567-e89b-12d3-a456-426614174000"

    def test_a_redemption_code_is_locked_marked_used_and_read_back(self, app_database):
        seed_user(app_database, 1)
        in_transaction(lambda c: charge_repository.insert_code(c, self.CODE, 100))

        async def work(connection):
            before = await charge_repository.lock_redemption_code(connection, self.CODE)
            await charge_repository.mark_code_used(
                connection, before.id, 1, datetime(2026, 10, 5, 9, 0, 0)
            )
            after = await charge_repository.lock_redemption_code(connection, self.CODE)
            return before, after

        before, after = in_transaction(work)

        assert (before.amount, before.is_used, before.used_by, before.used_at) == (
            100,
            False,
            None,
            None,
        )
        assert (after.is_used, after.used_by, after.used_at) == (
            True,
            1,
            datetime(2026, 10, 5, 9, 0, 0),
        )

    def test_unknown_codes_are_not_found(self, app_database):
        async def work(connection):
            return (
                await charge_repository.lock_redemption_code(connection, self.CODE),
                await charge_repository.code_exists(connection, self.CODE),
            )

        assert in_transaction(work) == (None, False)

    def test_code_existence_sees_inserted_codes(self, app_database):
        in_transaction(lambda c: charge_repository.insert_code(c, self.CODE, 5))

        async def work(connection):
            return await charge_repository.code_exists(connection, self.CODE)

        assert in_transaction(work) is True

    def test_a_topup_request_is_pending_until_it_is_claimed_once(self, app_database):
        seed_user(app_database, 1)
        request_id = in_transaction(lambda c: charge_repository.insert_topup_request(c, 1, 50, 199))

        request = run(charge_repository.get_topup_request(request_id))
        assert request == charge_repository.TopupRequest(request_id, 1, 50, 199, "pending")

        now = datetime(2026, 10, 5, 9, 0, 0)

        async def claim(connection, status):
            return await charge_repository.claim_pending_topup_request(
                connection, request_id, status, now, 900
            )

        assert in_transaction(lambda c: claim(c, "approved")) is True
        assert in_transaction(lambda c: claim(c, "rejected")) is False
        assert run(charge_repository.get_topup_request(request_id)).status == "approved"
        row = fetch(app_database, "SELECT decided_at, decided_by FROM topup_requests")[0]
        assert (row["decided_at"], row["decided_by"]) == (now, 900)

    def test_the_locked_status_reflects_the_latest_committed_state(self, app_database):
        seed_user(app_database, 1)
        request_id = in_transaction(lambda c: charge_repository.insert_topup_request(c, 1, 50, 199))
        in_transaction(
            lambda c: charge_repository.claim_pending_topup_request(
                c, request_id, "blocked", datetime(2026, 10, 5), 900
            )
        )

        async def work(connection):
            return (
                await charge_repository.lock_topup_status(connection, request_id),
                await charge_repository.lock_topup_status(connection, 9999),
            )

        assert in_transaction(work) == ("blocked", None)

    def test_only_pending_requests_can_be_discarded(self, app_database):
        seed_user(app_database, 1)
        pending = in_transaction(lambda c: charge_repository.insert_topup_request(c, 1, 50, 199))
        decided = in_transaction(lambda c: charge_repository.insert_topup_request(c, 1, 100, 299))
        in_transaction(
            lambda c: charge_repository.claim_pending_topup_request(
                c, decided, "approved", datetime(2026, 10, 5), 900
            )
        )

        removed_pending = in_transaction(
            lambda c: charge_repository.delete_pending_topup_request(c, pending)
        )
        removed_decided = in_transaction(
            lambda c: charge_repository.delete_pending_topup_request(c, decided)
        )

        assert (removed_pending, removed_decided) == (1, 0)
        assert run(charge_repository.get_topup_request(pending)) is None
        assert run(charge_repository.get_topup_request(decided)) is not None

    def test_the_recharge_block_round_trips(self, app_database):
        seed_user(app_database, 1)
        assert run(charge_repository.get_recharge_blocked_until(1)) is None
        assert run(charge_repository.get_recharge_blocked_until(404)) is None
        until = datetime(2026, 10, 6, 9, 0, 0)

        in_transaction(lambda c: charge_repository.set_recharge_blocked_until(c, 1, until))

        assert run(charge_repository.get_recharge_blocked_until(1)) == until


class TestWebPasswordRepository:
    def test_saving_twice_updates_the_hash_and_keeps_the_creation_time(self, app_database):
        seed_user(app_database, 1)
        assert run(web_password_repository.get_web_password(1)) is None

        in_transaction(lambda c: web_password_repository.save_web_password(c, 1, "hash-one"))
        first = run(web_password_repository.get_web_password(1))
        execute(
            app_database,
            "UPDATE web_password SET created_at = '2020-01-01 00:00:00' WHERE user_id = 1",
        )
        in_transaction(lambda c: web_password_repository.save_web_password(c, 1, "hash-two"))
        second = run(web_password_repository.get_web_password(1))

        assert first.password_hash == "hash-one"
        assert second.password_hash == "hash-two"
        assert second.created_at == datetime(2020, 1, 1, 0, 0, 0)
        assert fetch_scalar(app_database, "SELECT COUNT(*) FROM web_password") == 1


class TestLotteryRepository:
    def test_the_timestamp_is_upserted(self, app_database):
        seed_user(app_database, 1)
        assert run(lottery_repository.get_last_lottery_date(1)) is None
        first = datetime(2026, 10, 4, 8, 0, 0)
        second = datetime(2026, 10, 5, 8, 0, 0)

        in_transaction(lambda c: lottery_repository.save_last_lottery_date(c, 1, first))
        in_transaction(lambda c: lottery_repository.save_last_lottery_date(c, 1, second))

        assert run(lottery_repository.get_last_lottery_date(1)) == second
        assert fetch_scalar(app_database, "SELECT COUNT(*) FROM user_lottery") == 1
