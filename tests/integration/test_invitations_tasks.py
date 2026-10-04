"""邀请奖励、新用户开户奖励与任务奖励：奖励幂等、与业务状态同事务（真实 MySQL）。"""

from types import SimpleNamespace

import pytest
from economy_support import (
    Recorder,
    gather_all,
    ledger_keys,
    ledger_rows,
    make_callback_update,
    make_command_update,
    make_context,
    seed_user,
    user_state,
)
from mysql_support import execute, fetch, fetch_scalar, run

from core import balance, config, sql
from features.economy import ref as ref_handlers, task as task_handlers
from features.economy.operations import invitations as ref
from features.economy.operations import task
from features.economy.operations.task import TaskClaim
from features.profile import handlers as profile

BONUS = config.NEW_USER_BONUS_COINS
REWARD = ref.INVITATION_REWARD
me_command = profile.me.__wrapped__
ref_command = ref_handlers.ref_command.__wrapped__


def total(url, user_id):
    state = user_state(url, user_id)
    return state["free"] + state["paid"]


def invitations(url):
    return fetch(
        url,
        "SELECT invited_user_id, referrer_id, reward_claimed FROM user_invitations "
        "ORDER BY invited_user_id",
    )


def user_exists(url, user_id):
    return fetch_scalar(url, "SELECT COUNT(*) FROM `user` WHERE id = %s", (user_id,)) == 1


class TestInvitationRewards:
    def test_a_new_invitee_gets_the_signup_bonus_and_the_invitation_reward(self, app_database):
        seed_user(app_database, 1, free=0, name="referrer")

        result = run(ref.add_invitation_record(10, 1, "Newbie"))

        assert result == (True, True)
        assert total(app_database, 10) == BONUS + REWARD
        assert total(app_database, 1) == REWARD
        assert ledger_keys(app_database, 10) == ["signup:10", "ref_invitee:10"]
        assert ledger_keys(app_database, 1) == ["ref_referrer:10"]
        assert invitations(app_database) == [
            {"invited_user_id": 10, "referrer_id": 1, "reward_claimed": 1}
        ]
        assert fetch_scalar(app_database, "SELECT name FROM `user` WHERE id = 10") == "Newbie"
        assert run(balance.audit_ledger()).clean

    def test_an_existing_user_only_gets_the_invitation_reward(self, app_database):
        seed_user(app_database, 1)
        seed_user(app_database, 10, free=5)

        result = run(ref.add_invitation_record(10, 1, "Old"))

        assert result == (True, False)
        assert total(app_database, 10) == 5 + REWARD
        assert ledger_keys(app_database, 10) == ["ref_invitee:10"]

    def test_an_invitee_can_only_be_invited_once(self, app_database):
        seed_user(app_database, 1)
        seed_user(app_database, 3)
        run(ref.add_invitation_record(10, 1, "Newbie"))

        again_same = run(ref.add_invitation_record(10, 1, "Newbie"))
        again_other = run(ref.add_invitation_record(10, 3, "Newbie"))

        assert again_same == (False, False)
        assert again_other == (False, False)
        assert total(app_database, 10) == BONUS + REWARD
        assert total(app_database, 1) == REWARD
        assert total(app_database, 3) == 0
        assert len(invitations(app_database)) == 1

    def test_a_missing_referrer_creates_nothing(self, app_database):
        result = run(ref.add_invitation_record(10, 404, "Newbie"))

        assert result == (False, False)
        assert not user_exists(app_database, 10)
        assert ledger_rows(app_database) == []
        assert invitations(app_database) == []

    def test_without_a_signup_bonus_the_new_invitee_only_gets_the_invitation_reward(
        self, app_database, monkeypatch
    ):
        monkeypatch.setattr(config, "NEW_USER_BONUS_COINS", 0)
        seed_user(app_database, 1)

        run(ref.add_invitation_record(10, 1, "Newbie"))

        assert total(app_database, 10) == REWARD
        assert ledger_keys(app_database, 10) == ["ref_invitee:10"]

    def test_concurrent_invitations_of_one_new_user_reward_exactly_once(self, app_database):
        for referrer in (1, 2, 3, 4, 5):
            seed_user(app_database, referrer)

        async def scenario():
            return await gather_all(
                *[ref.add_invitation_record(10, referrer, "Newbie") for referrer in (1, 2, 3, 4, 5)]
            )

        results = run(scenario())

        assert all(not isinstance(item, Exception) for item in results), results
        assert sorted(item[0] for item in results) == [False] * 4 + [True]
        assert total(app_database, 10) == BONUS + REWARD
        assert sum(total(app_database, referrer) for referrer in (1, 2, 3, 4, 5)) == REWARD
        assert len(invitations(app_database)) == 1
        assert ledger_keys(app_database, 10).count("ref_invitee:10") == 1

    def test_concurrent_invitations_of_an_existing_user_reward_exactly_once(self, app_database):
        for referrer in (1, 2, 3, 4):
            seed_user(app_database, referrer)
        seed_user(app_database, 10, free=1)

        async def scenario():
            return await gather_all(
                *[ref.add_invitation_record(10, referrer, "Old") for referrer in (1, 2, 3, 4)]
            )

        results = run(scenario())

        assert all(not isinstance(item, Exception) for item in results), results
        assert [item[0] for item in results].count(True) == 1
        assert total(app_database, 10) == 1 + REWARD
        assert sum(total(app_database, referrer) for referrer in (1, 2, 3, 4)) == REWARD

    def test_a_first_invitation_burst_for_different_new_users_all_succeed(self, app_database):
        seed_user(app_database, 1)

        async def scenario():
            return await gather_all(
                *[ref.add_invitation_record(100 + index, 1, f"New{index}") for index in range(8)]
            )

        results = run(scenario())

        assert all(not isinstance(item, Exception) for item in results), results
        assert {item for item in results} == {(True, True)}
        assert total(app_database, 1) == 8 * REWARD

    def test_mutual_invitations_at_the_same_time_do_not_deadlock(self, app_database, monkeypatch):
        deadlocks = []
        real_is_deadlock = sql.is_deadlock_error

        def recording_is_deadlock(exc):
            found = real_is_deadlock(exc)
            if found:
                deadlocks.append(exc)
            return found

        monkeypatch.setattr(sql, "is_deadlock_error", recording_is_deadlock)
        for user_id in range(1, 7):
            seed_user(app_database, user_id)

        async def scenario():
            jobs = []
            for low, high in ((1, 2), (3, 4), (5, 6)):
                jobs.append(ref.add_invitation_record(low, high, "x"))
                jobs.append(ref.add_invitation_record(high, low, "x"))
            return await gather_all(*jobs)

        results = run(scenario())

        assert all(not isinstance(item, Exception) for item in results), results
        assert {item[0] for item in results} == {True}
        assert deadlocks == []
        assert all(total(app_database, user_id) == 2 * REWARD for user_id in range(1, 7))

    def test_a_failure_while_rewarding_the_referrer_rolls_everything_back(
        self, app_database, monkeypatch
    ):
        seed_user(app_database, 1)
        real_credit = balance.credit

        async def failing_credit(connection, user_id, amount, **kwargs):
            if kwargs["reason"] == "ref_referrer":
                raise RuntimeError("入账失败")
            return await real_credit(connection, user_id, amount, **kwargs)

        monkeypatch.setattr(balance, "credit", failing_credit)
        failed = run(ref.add_invitation_record(10, 1, "Newbie"))
        monkeypatch.setattr(balance, "credit", real_credit)

        assert failed == (False, False)
        assert not user_exists(app_database, 10)
        assert invitations(app_database) == []
        assert ledger_rows(app_database) == []

        # 没有留下「已邀请」的记录：重试成功，奖励只发一次。
        retry = run(ref.add_invitation_record(10, 1, "Newbie"))
        assert retry == (True, True)
        assert total(app_database, 1) == REWARD

    def test_binding_a_referrer_with_the_command_rewards_both_sides(self, app_database):
        seed_user(app_database, 1)
        update = make_command_update(user_id=10, message_id=3)
        update.effective_user.full_name = "Newbie"
        context = make_context()
        context.args = ["1"]

        run(ref_command(update, context))

        assert "邀请绑定成功" in update.message.reply_text.texts[0]
        assert total(app_database, 10) == BONUS + REWARD
        assert total(app_database, 1) == REWARD


class TestSignupBonus:
    def me(self, url, user_id=1):
        update = make_command_update(user_id=user_id, message_id=3)
        run(me_command(update, make_context()))
        return update

    def test_the_first_me_grants_the_bonus_through_the_ledger(self, app_database):
        update = self.me(app_database)

        assert total(app_database, 1) == BONUS
        assert ledger_keys(app_database) == ["signup:1"]
        assert f"总额 Total: {BONUS}" in update.message.reply_text.texts[0]

    def test_later_me_calls_do_not_pay_again(self, app_database):
        self.me(app_database)
        self.me(app_database)

        assert total(app_database, 1) == BONUS
        assert ledger_keys(app_database) == ["signup:1"]

    def test_a_user_who_registered_before_the_ledger_existed_gets_no_bonus(self, app_database):
        seed_user(app_database, 1, free=3, name="user1")

        self.me(app_database)

        assert total(app_database, 1) == 3
        assert ledger_rows(app_database) == []

    def test_concurrent_first_me_calls_pay_once(self, app_database):
        def one_call():
            update = make_command_update(user_id=1, message_id=3)
            return me_command(update, make_context())

        async def scenario():
            return await gather_all(*[one_call() for _ in range(5)])

        results = run(scenario())

        assert all(not isinstance(item, Exception) for item in results), results
        assert total(app_database, 1) == BONUS
        assert ledger_keys(app_database) == ["signup:1"]

    def test_a_user_opened_by_an_invitation_is_not_paid_again_by_me(self, app_database):
        seed_user(app_database, 1)
        run(ref.add_invitation_record(10, 1, "Newbie"))

        self.me(app_database, user_id=10)

        assert total(app_database, 10) == BONUS + REWARD
        assert ledger_keys(app_database, 10) == ["signup:10", "ref_invitee:10"]

    def test_a_disabled_bonus_registers_without_a_ledger_row(self, app_database, monkeypatch):
        monkeypatch.setattr(config, "NEW_USER_BONUS_COINS", 0)

        self.me(app_database)

        assert user_exists(app_database, 1)
        assert total(app_database, 1) == 0
        assert ledger_rows(app_database) == []


class TestTaskReward:
    def test_claiming_pays_the_reward_and_records_completion_together(self, app_database):
        seed_user(app_database, 1)

        status = run(task.claim_task_reward(1, task.TASK_ID_CHECK_GROUP1, 10))

        assert status is TaskClaim.CLAIMED
        assert total(app_database, 1) == 10
        assert ledger_keys(app_database) == ["task:1:1"]
        assert fetch_scalar(app_database, "SELECT COUNT(*) FROM user_task") == 1

    def test_a_task_pays_only_once(self, app_database):
        seed_user(app_database, 1)
        run(task.claim_task_reward(1, 1, 10))

        again = run(task.claim_task_reward(1, 1, 10))
        other_task = run(task.claim_task_reward(1, 2, 10))

        assert again is TaskClaim.ALREADY_DONE
        assert other_task is TaskClaim.CLAIMED
        assert total(app_database, 1) == 20

    def test_concurrent_claims_pay_once(self, app_database):
        seed_user(app_database, 1)

        async def scenario():
            return await gather_all(*[task.claim_task_reward(1, 1, 10) for _ in range(6)])

        results = run(scenario())

        assert all(not isinstance(item, Exception) for item in results), results
        assert results.count(TaskClaim.CLAIMED) == 1
        assert results.count(TaskClaim.ALREADY_DONE) == 5
        assert total(app_database, 1) == 10

    def test_an_unregistered_user_gets_no_reward_and_no_completion_record(self, app_database):
        status = run(task.claim_task_reward(404, 1, 10))

        assert status is TaskClaim.NOT_REGISTERED
        assert fetch_scalar(app_database, "SELECT COUNT(*) FROM user_task") == 0
        assert ledger_rows(app_database) == []

    def test_a_failure_after_the_credit_leaves_no_reward_and_no_completion(
        self, app_database, monkeypatch
    ):
        seed_user(app_database, 1)
        real_credit = balance.credit

        async def credit_then_fail(*args, **kwargs):
            await real_credit(*args, **kwargs)
            raise RuntimeError("injected failure")

        monkeypatch.setattr(balance, "credit", credit_then_fail)
        with pytest.raises(RuntimeError):
            run(task.claim_task_reward(1, 1, 10))
        monkeypatch.setattr(balance, "credit", real_credit)

        assert total(app_database, 1) == 0
        assert fetch_scalar(app_database, "SELECT COUNT(*) FROM user_task") == 0
        # 完成记录没有落库：重试仍然可以领取。
        assert run(task.claim_task_reward(1, 1, 10)) is TaskClaim.CLAIMED
        assert total(app_database, 1) == 10

    def test_a_manually_cleared_completion_record_does_not_pay_again(self, app_database):
        seed_user(app_database, 1)
        run(task.claim_task_reward(1, 1, 10))
        execute(app_database, "DELETE FROM user_task")

        again = run(task.claim_task_reward(1, 1, 10))

        assert again is TaskClaim.ALREADY_DONE
        assert total(app_database, 1) == 10
        assert fetch_scalar(app_database, "SELECT COUNT(*) FROM user_task") == 1

    def click(self, *, member_status="member", user_id=1):
        update, answer, _ = make_callback_update(from_user_id=user_id, data="task_check_group1")
        context = make_context(get_chat_member=Recorder(result=SimpleNamespace(status=member_status)))
        run(task_handlers.task_callback(update, context))
        return answer.texts

    def test_the_button_pays_a_group_member(self, app_database):
        seed_user(app_database, 1)

        texts = self.click()

        assert texts == ["恭喜您完成任务，获得 10 个硬币奖励！"]
        assert total(app_database, 1) == 10

    def test_the_button_pays_only_once(self, app_database):
        seed_user(app_database, 1)
        self.click()

        texts = self.click()

        assert texts == ["您已完成该任务，不能重复领取奖励。"]
        assert total(app_database, 1) == 10

    def test_someone_who_left_the_group_is_not_paid(self, app_database):
        seed_user(app_database, 1)

        texts = self.click(member_status="left")

        assert "尚未加入" in texts[0]
        assert total(app_database, 1) == 0

    def test_an_unregistered_user_is_asked_to_register_instead_of_marked_done(self, app_database):
        texts = self.click(user_id=404)

        assert texts == ["请先使用 /me 命令获取个人信息。"]
        assert fetch_scalar(app_database, "SELECT COUNT(*) FROM user_task") == 0
