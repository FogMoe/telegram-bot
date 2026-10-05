"""定时任务的 claim 所有权、租约、阶段与崩溃恢复（真实 MySQL）。

约定见 docs/job-recovery.md。“崩溃”用 SimulatedCrash（BaseException）模拟：
它跳过所有 `except Exception`，数据库停在崩溃那一刻。
"""

import asyncio
from types import SimpleNamespace

import pytest
from job_support import (
    FakeAI,
    FakeBot,
    FakeTelegram,
    SimulatedCrash,
    a_execute,
    a_fetch,
    attempts,
    expire_lease,
    schedule_row,
    seed_schedule,
    seed_user,
)
from mysql_support import execute, fetch, fetch_scalar, run

from fogmoe_telegram_bot.features.ai import conversation_locks, job_claims, scheduler
from fogmoe_telegram_bot.features.ai.tools import schedule_tools

REPLY = "drink some water"


@pytest.fixture
def env(app_database, monkeypatch):
    telegram = FakeTelegram()
    ai = FakeAI(REPLY)

    async def no_media(**kwargs):
        return []

    monkeypatch.setattr(scheduler.ai_chat, "get_ai_response", ai.get_ai_response)
    monkeypatch.setattr(scheduler, "send_ai_reply_with_stickers", telegram.send_reply)
    monkeypatch.setattr(scheduler, "send_generated_media", no_media)
    monkeypatch.setattr(scheduler.summary, "schedule_summary_generation", lambda *a, **k: None)
    # 心跳留给专门的测试；其余测试里租约只靠手工到期。
    monkeypatch.setattr(scheduler, "SCHEDULE_HEARTBEAT_SECONDS", 3600)
    # asyncio.Lock 一旦发生过竞争就绑定了那个事件循环；每个测试用自己的会话锁表。
    monkeypatch.setattr(conversation_locks, "_CONVERSATION_LOCKS", {})
    return SimpleNamespace(
        url=app_database,
        ai=ai,
        telegram=telegram,
        context=SimpleNamespace(bot=FakeBot()),
    )


def poll(env):
    run(scheduler.run_ai_schedule_job(env.context))


def expire(env, schedule_id):
    expire_lease(env.url, "ai_schedules", "id", schedule_id)


def outcomes(env, schedule_id):
    return [row["outcome"] for row in attempts(env.url, "schedule", schedule_id)]


def row_snapshot(row):
    return {
        key: row[key]
        for key in ("status", "stage", "run_at", "last_run_at", "error", "claim_token", "claim_attempts")
    }


def crash_in_build_user_state_prompt(monkeypatch, *, only_user=None):
    """让 claim 之后、任何副作用之前的第一步崩溃；返回开关。"""
    original = scheduler.build_user_state_prompt
    state = {"crash": True}

    async def crashing(user_id):
        if state["crash"] and (only_user is None or user_id == only_user):
            raise SimulatedCrash("process died right after claiming")
        return await original(user_id)

    monkeypatch.setattr(scheduler, "build_user_state_prompt", crashing)
    return state


class TestCrashRecovery:
    def test_crash_before_side_effects_is_reclaimed_with_a_new_token_and_runs_once(
        self, env, monkeypatch
    ):
        seed_user(env.url, 1)
        schedule_id = seed_schedule(env.url, 1)
        state = crash_in_build_user_state_prompt(monkeypatch)

        with pytest.raises(SimulatedCrash):
            poll(env)

        crashed = schedule_row(env.url, schedule_id)
        assert crashed["status"] == "executing"
        assert crashed["stage"] == "claimed"
        first_token = crashed["claim_token"]
        assert len(first_token) == 32

        # 租约还没到期：别的轮询不会碰它，也不会重复执行。
        state["crash"] = False
        poll(env)
        assert schedule_row(env.url, schedule_id)["claim_token"] == first_token
        assert env.ai.calls == 0

        expire(env, schedule_id)
        poll(env)

        done = schedule_row(env.url, schedule_id)
        assert done["status"] == "executed"
        assert done["claim_token"] is None and done["claim_until"] is None
        assert done["stage"] == "idle" and done["claim_attempts"] == 0
        assert env.ai.calls == 1
        assert env.telegram.delivered == [REPLY]
        rows = attempts(env.url, "schedule", schedule_id)
        assert [row["outcome"] for row in rows] == ["expired", "completed"]
        assert [row["attempt_no"] for row in rows] == [1, 2]
        assert rows[0]["claim_token"] == first_token
        assert rows[1]["claim_token"] != first_token
        assert rows[1]["stage"] == "completed"

    @pytest.mark.parametrize(
        ("unit", "expected_status"), [("none", "failed"), ("hour", "pending")]
    )
    def test_crash_after_telegram_accepted_the_message_is_not_resent(
        self, env, unit, expected_status
    ):
        """外部操作成功、本地结果还没记录：按结果未知处理，不重发、不重跑。"""
        seed_user(env.url, 1)
        schedule_id = seed_schedule(env.url, 1, unit=unit)
        env.telegram.crash_after_send = True

        with pytest.raises(SimulatedCrash):
            poll(env)

        crashed = schedule_row(env.url, schedule_id)
        assert crashed["status"] == "executing"
        assert crashed["stage"] == "delivering"
        assert env.telegram.delivered == [REPLY]

        env.telegram.crash_after_send = False
        expire(env, schedule_id)
        poll(env)
        # 再来一轮也不会重新执行。
        poll(env)

        after = schedule_row(env.url, schedule_id)
        assert after["status"] == expected_status
        assert "outcome is unknown" in after["error"]
        assert after["claim_token"] is None and after["stage"] == "idle"
        assert env.ai.calls == 1
        assert env.telegram.delivered == [REPLY]
        rows = attempts(env.url, "schedule", schedule_id)
        assert [row["outcome"] for row in rows] == ["unknown"]
        assert rows[0]["stage"] == "delivering"
        assert rows[0]["daily_trigger_reserved"] == 1
        if unit != "none":
            # 循环任务只推进一次，并且仍然是 pending。
            assert after["last_run_at"] == crashed["run_at"]
            assert after["run_at"] == scheduler._calculate_next_run_at(
                crashed["run_at"], unit, 1
            )

    def test_crash_during_generation_is_not_replayed(self, env):
        seed_user(env.url, 1)
        schedule_id = seed_schedule(env.url, 1)

        async def die(tool_context):
            raise SimulatedCrash("process died while tools were running")

        env.ai.hook = die
        with pytest.raises(SimulatedCrash):
            poll(env)
        assert schedule_row(env.url, schedule_id)["stage"] == "generating"

        env.ai.hook = None
        expire(env, schedule_id)
        poll(env)

        after = schedule_row(env.url, schedule_id)
        assert after["status"] == "failed"
        assert "Interrupted during generating" in after["error"]
        assert env.ai.calls == 1
        assert env.telegram.delivered == []
        assert outcomes(env, schedule_id) == ["unknown"]

    def test_a_task_that_keeps_crashing_before_it_starts_is_abandoned(self, env, monkeypatch):
        seed_user(env.url, 1)
        schedule_id = seed_schedule(env.url, 1)
        crash_in_build_user_state_prompt(monkeypatch)

        for _ in range(scheduler.SCHEDULE_MAX_CLAIM_ATTEMPTS):
            with pytest.raises(SimulatedCrash):
                poll(env)
            expire(env, schedule_id)
        poll(env)

        after = schedule_row(env.url, schedule_id)
        assert after["status"] == "failed"
        assert "Abandoned after 3 claims" in after["error"]
        assert outcomes(env, schedule_id) == ["expired", "expired", "abandoned"]
        assert env.ai.calls == 0

    def test_a_crash_does_not_strand_the_rest_of_the_batch(self, env, monkeypatch):
        for user_id, overdue in ((1, 30), (2, 20), (3, 10)):
            seed_user(env.url, user_id)
        ids = [
            seed_schedule(env.url, 1, minutes_overdue=30),
            seed_schedule(env.url, 2, minutes_overdue=20),
            seed_schedule(env.url, 3, minutes_overdue=10),
        ]
        state = crash_in_build_user_state_prompt(monkeypatch, only_user=1)

        with pytest.raises(SimulatedCrash):
            poll(env)

        # 只有当前任务被卡住；同一批里的其他任务根本没有被 claim。
        assert schedule_row(env.url, ids[0])["status"] == "executing"
        for other in ids[1:]:
            row = schedule_row(env.url, other)
            assert row["status"] == "pending" and row["claim_token"] is None

        # 不用等租约到期，下一次轮询就会处理它们。
        poll(env)
        assert schedule_row(env.url, ids[0])["status"] == "executing"
        assert [schedule_row(env.url, other)["status"] for other in ids[1:]] == [
            "executed",
            "executed",
        ]
        assert env.ai.calls == 2

        # 被卡住的那个在租约到期后被回收。
        state["crash"] = False
        expire(env, ids[0])
        poll(env)
        assert schedule_row(env.url, ids[0])["status"] == "executed"
        assert env.ai.calls == 3


class TestClaiming:
    def test_concurrent_pollers_never_claim_the_same_task(self, env):
        seed_user(env.url, 1)
        schedule_id = seed_schedule(env.url, 1)

        async def scenario():
            return await asyncio.gather(
                scheduler._claim_next_schedule(),
                scheduler._claim_next_schedule(),
                scheduler._claim_next_schedule(),
            )

        claims = [claim for claim in run(scenario()) if claim is not None]

        assert len(claims) == 1
        assert claims[0].schedule_id == schedule_id
        assert schedule_row(env.url, schedule_id)["claim_token"] == claims[0].token


class TestStaleWorker:
    @pytest.mark.parametrize("unit", ["none", "hour"])
    def test_a_stale_worker_that_finishes_late_is_rejected(self, env, unit):
        """租约到期并被回收后，旧 worker 迟到完成：不投递、不再写状态、不再推进循环任务。"""
        seed_user(env.url, 1)
        schedule_id = seed_schedule(env.url, 1, unit=unit)
        snapshots = {}

        async def scenario():
            started, release = asyncio.Event(), asyncio.Event()

            async def slow(tool_context):
                started.set()
                await release.wait()

            env.ai.hook = slow
            claim = await scheduler._claim_next_schedule()
            worker = asyncio.create_task(scheduler._process_schedule_task(claim, env.context))
            await started.wait()

            await a_execute(
                "UPDATE ai_schedules SET claim_until = UTC_TIMESTAMP() - INTERVAL 5 SECOND "
                "WHERE id = %s",
                (schedule_id,),
            )
            assert await scheduler._recover_expired_schedules() == 1
            snapshots["after_recovery"] = (
                await a_fetch(
                    "SELECT status, stage, run_at, last_run_at, error, claim_token "
                    "FROM ai_schedules WHERE id = %s",
                    (schedule_id,),
                )
            )[0]
            release.set()
            await worker
            snapshots["after_late_worker"] = (
                await a_fetch(
                    "SELECT status, stage, run_at, last_run_at, error, claim_token "
                    "FROM ai_schedules WHERE id = %s",
                    (schedule_id,),
                )
            )[0]

        run(scenario())

        assert env.telegram.delivered == []
        assert tuple(snapshots["after_late_worker"]) == tuple(snapshots["after_recovery"])
        assert outcomes(env, schedule_id) == ["unknown"]
        row = schedule_row(env.url, schedule_id)
        assert row["status"] == ("pending" if unit == "hour" else "failed")
        if unit == "hour":
            assert row["run_at"] == scheduler._calculate_next_run_at(
                row["last_run_at"], "hour", 1
            )

    def test_a_stale_worker_that_was_requeued_cannot_start_or_complete(self, env, monkeypatch):
        """claimed 阶段卡住的 worker 被回收、新 token 的 worker 完成之后，旧 worker 恢复也什么都做不了。"""
        seed_user(env.url, 1)
        schedule_id = seed_schedule(env.url, 1, unit="hour")
        original = scheduler.build_user_state_prompt
        gate = {"first": True}
        snapshots = {}

        async def scenario():
            started, release = asyncio.Event(), asyncio.Event()

            async def stuck_once(user_id):
                if gate["first"]:
                    gate["first"] = False
                    started.set()
                    await release.wait()
                return await original(user_id)

            monkeypatch.setattr(scheduler, "build_user_state_prompt", stuck_once)
            claim_a = await scheduler._claim_next_schedule()
            worker_a = asyncio.create_task(scheduler._process_schedule_task(claim_a, env.context))
            await started.wait()

            # 另一个进程：租约到期、回收、用新 token 重新执行。每个进程有自己的会话锁。
            monkeypatch.setattr(scheduler, "get_conversation_lock", lambda _id: asyncio.Lock())
            await a_execute(
                "UPDATE ai_schedules SET claim_until = UTC_TIMESTAMP() - INTERVAL 5 SECOND "
                "WHERE id = %s",
                (schedule_id,),
            )
            assert await scheduler._recover_expired_schedules() == 1
            claim_b = await scheduler._claim_next_schedule()
            assert claim_b.token != claim_a.token and claim_b.attempt == 2
            await scheduler._process_schedule_task(claim_b, env.context)
            query = (
                "SELECT status, stage, run_at, last_run_at, error, claim_token "
                "FROM ai_schedules WHERE id = %s"
            )
            snapshots["after_b"] = tuple((await a_fetch(query, (schedule_id,)))[0])
            release.set()
            await worker_a
            snapshots["after_a"] = tuple((await a_fetch(query, (schedule_id,)))[0])

        run(scenario())

        assert snapshots["after_a"] == snapshots["after_b"]
        assert env.ai.calls == 1
        assert env.telegram.delivered == [REPLY]
        assert outcomes(env, schedule_id) == ["expired", "completed"]

    def test_old_claim_cannot_write_after_the_task_was_reclaimed(self, env):
        seed_user(env.url, 1)
        schedule_id = seed_schedule(env.url, 1)

        async def scenario():
            old = await scheduler._claim_next_schedule()
            await a_execute(
                "UPDATE ai_schedules SET claim_until = UTC_TIMESTAMP() - INTERVAL 5 SECOND "
                "WHERE id = %s",
                (schedule_id,),
            )
            await scheduler._recover_expired_schedules()
            new = await scheduler._claim_next_schedule()
            assert new.token != old.token

            old_run = scheduler._ScheduleRun(old)
            with pytest.raises(job_claims.ClaimLostError):
                await scheduler._begin_generation(old_run)
            with pytest.raises(job_claims.ClaimLostError):
                await scheduler._begin_delivery(old_run)
            with pytest.raises(job_claims.ClaimLostError):
                await scheduler._complete_claim(old_run)
            with pytest.raises(job_claims.ClaimLostError):
                await scheduler._release_claim(
                    old_run, "released", attempts_expr=scheduler._ATTEMPTS_RESET
                )
            assert not await job_claims.renew_lease(
                job_claims.SCHEDULE_JOB, schedule_id, old.token, 60
            )

            # 新 claim 不受影响。
            assert await job_claims.renew_lease(
                job_claims.SCHEDULE_JOB, schedule_id, new.token, 60
            )

        run(scenario())

        row = schedule_row(env.url, schedule_id)
        assert row["status"] == "executing" and row["stage"] == "claimed"
        assert row["claim_attempts"] == 2

    def test_stage_transitions_require_a_live_lease(self, env):
        """租约都确认不了的 worker 不应该开始产生副作用。"""
        seed_user(env.url, 1)
        schedule_id = seed_schedule(env.url, 1)

        async def scenario():
            claim = await scheduler._claim_next_schedule()
            await a_execute(
                "UPDATE ai_schedules SET claim_until = UTC_TIMESTAMP() - INTERVAL 5 SECOND "
                "WHERE id = %s",
                (schedule_id,),
            )
            with pytest.raises(job_claims.ClaimLostError):
                await scheduler._begin_generation(scheduler._ScheduleRun(claim))

        run(scenario())

        assert fetch_scalar(env.url, "SELECT ai_schedule_trigger_count FROM `user` WHERE id = 1") == 0
        assert schedule_row(env.url, schedule_id)["stage"] == "claimed"


class TestLease:
    def test_heartbeat_keeps_a_long_running_claim_alive(self, env, monkeypatch):
        monkeypatch.setattr(scheduler, "SCHEDULE_LEASE_SECONDS", 2)
        monkeypatch.setattr(scheduler, "SCHEDULE_HEARTBEAT_SECONDS", 0.3)
        seed_user(env.url, 1)
        schedule_id = seed_schedule(env.url, 1)
        recovered_while_running = []

        async def slow(tool_context):
            # 比 2 秒的租约长：没有续期的话，这期间的回收会把它当成已经死掉。
            await asyncio.sleep(3.2)

        env.ai.hook = slow

        async def scenario():
            claim = await scheduler._claim_next_schedule()
            worker = asyncio.create_task(scheduler._process_schedule_task(claim, env.context))
            await asyncio.sleep(2.6)
            recovered_while_running.append(await scheduler._recover_expired_schedules())
            await worker

        run(scenario())

        assert recovered_while_running == [0]
        assert schedule_row(env.url, schedule_id)["status"] == "executed"
        assert env.telegram.delivered == [REPLY]
        assert outcomes(env, schedule_id) == ["completed"]

    def test_worker_stops_when_the_lease_can_no_longer_be_renewed(self, env, monkeypatch):
        monkeypatch.setattr(scheduler, "SCHEDULE_HEARTBEAT_SECONDS", 0.2)
        seed_user(env.url, 1)
        schedule_id = seed_schedule(env.url, 1)
        observed = {}

        async def hang(tool_context):
            observed["abort_event"] = tool_context["abort_event"]
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                observed["cancelled"] = True
                raise

        env.ai.hook = hang

        async def scenario():
            claim = await scheduler._claim_next_schedule()
            worker = asyncio.create_task(scheduler._process_schedule_task(claim, env.context))
            while "abort_event" not in observed:
                await asyncio.sleep(0.05)
            # 别的进程已经拿走了这个任务。
            await a_execute(
                "UPDATE ai_schedules SET claim_token = %s WHERE id = %s",
                ("f" * 32, schedule_id),
            )
            await asyncio.wait_for(worker, timeout=5)

        run(scenario())

        assert observed["cancelled"] is True
        assert observed["abort_event"].is_set()
        assert env.telegram.delivered == []
        row = schedule_row(env.url, schedule_id)
        # 失去所有权的 worker 没有写任何状态。
        assert row["status"] == "executing" and row["claim_token"] == "f" * 32
        assert row["stage"] == "generating"

    def test_execution_limit_cancels_a_stuck_worker_and_records_the_failure(
        self, env, monkeypatch
    ):
        monkeypatch.setattr(scheduler, "SCHEDULE_EXECUTION_TIMEOUT_SECONDS", 0.5)
        seed_user(env.url, 1)
        schedule_id = seed_schedule(env.url, 1)
        observed = {}

        async def hang(tool_context):
            observed["abort_event"] = tool_context["abort_event"]
            await asyncio.Event().wait()

        env.ai.hook = hang
        poll(env)

        row = schedule_row(env.url, schedule_id)
        assert row["status"] == "failed"
        assert "TimeoutError" in row["error"]
        assert observed["abort_event"].is_set()
        rows = attempts(env.url, "schedule", schedule_id)
        assert [r["outcome"] for r in rows] == ["failed"]
        assert rows[0]["stage"] == "generating"
        assert env.telegram.delivered == []


class TestStagesAndOperationRecords:
    def test_quota_and_generating_stage_commit_in_one_transaction(self, env, monkeypatch):
        seed_user(env.url, 1)
        schedule_id = seed_schedule(env.url, 1)

        async def scenario():
            claim = await scheduler._claim_next_schedule()
            run_state = scheduler._ScheduleRun(claim)

            async def boom(*args, **kwargs):
                raise RuntimeError("database went away after the quota was reserved")

            with monkeypatch.context() as patch:
                patch.setattr(job_claims, "advance_stage", boom)
                with pytest.raises(RuntimeError):
                    await scheduler._begin_generation(run_state)
            return run_state

        run_state = run(scenario())

        # 事务整体回滚：额度没有被占用，阶段仍是 claimed。
        assert fetch_scalar(env.url, "SELECT ai_schedule_trigger_count FROM `user` WHERE id = 1") == 0
        assert schedule_row(env.url, schedule_id)["stage"] == "claimed"
        assert attempts(env.url, "schedule", schedule_id)[0]["daily_trigger_reserved"] == 0

        assert run(scheduler._begin_generation(run_state)) is True
        assert fetch_scalar(env.url, "SELECT ai_schedule_trigger_count FROM `user` WHERE id = 1") == 1
        assert schedule_row(env.url, schedule_id)["stage"] == "generating"
        row = attempts(env.url, "schedule", schedule_id)[0]
        assert row["stage"] == "generating" and row["daily_trigger_reserved"] == 1

    def test_attempt_record_follows_the_stages(self, env):
        seed_user(env.url, 1)
        schedule_id = seed_schedule(env.url, 1)
        seen = []

        async def watch(tool_context):
            seen.append(
                [
                    (r["stage"], r["outcome"])
                    for r in await a_fetch_attempts(schedule_id)
                ]
            )

        async def a_fetch_attempts(job_id):
            rows = await a_fetch(
                "SELECT stage, outcome FROM ai_job_attempts "
                "WHERE job_type = 'schedule' AND job_id = %s",
                (job_id,),
            )
            return [{"stage": r[0], "outcome": r[1]} for r in rows]

        env.ai.hook = watch
        poll(env)

        assert seen == [[("generating", None)]]
        final = attempts(env.url, "schedule", schedule_id)
        assert [(r["stage"], r["outcome"]) for r in final] == [("completed", "completed")]
        assert final[0]["finished_at"] is not None

    def test_daily_limit_reached_after_claiming_returns_the_task_to_pending(self, env):
        seed_user(env.url, 1)
        schedule_id = seed_schedule(env.url, 1)

        async def scenario():
            claim = await scheduler._claim_next_schedule()
            await a_execute(
                "UPDATE `user` SET ai_schedule_trigger_date = UTC_DATE(), "
                "ai_schedule_trigger_count = %s WHERE id = 1",
                (scheduler.DAILY_SCHEDULE_TRIGGER_LIMIT,),
            )
            await scheduler._process_schedule_task(claim, env.context)

        run(scenario())

        row = schedule_row(env.url, schedule_id)
        assert row["status"] == "pending" and row["claim_token"] is None
        assert row["stage"] == "idle" and row["claim_attempts"] == 0
        assert outcomes(env, schedule_id) == ["paused"]
        assert env.ai.calls == 0
        # 额度没有被多占一次。
        assert (
            fetch_scalar(env.url, "SELECT ai_schedule_trigger_count FROM `user` WHERE id = 1")
            == scheduler.DAILY_SCHEDULE_TRIGGER_LIMIT
        )

    def test_coins_exhausted_after_claiming_returns_the_task_to_pending(self, env):
        seed_user(env.url, 1)
        schedule_id = seed_schedule(env.url, 1)

        async def scenario():
            claim = await scheduler._claim_next_schedule()
            await a_execute("UPDATE `user` SET coins = 0, coins_paid = 0 WHERE id = 1")
            await scheduler._process_schedule_task(claim, env.context)

        run(scenario())

        row = schedule_row(env.url, schedule_id)
        assert row["status"] == "pending" and row["stage"] == "idle"
        assert outcomes(env, schedule_id) == ["paused"]
        assert env.ai.calls == 0

    def test_users_without_coins_or_over_the_daily_limit_are_not_claimed(self, env):
        seed_user(env.url, 1, coins=0)
        seed_user(env.url, 2)
        seed_schedule(env.url, 1)
        seed_schedule(env.url, 2)
        execute(
            env.url,
            (
                "UPDATE `user` SET ai_schedule_trigger_date = UTC_DATE(), "
                "ai_schedule_trigger_count = %s WHERE id = 2",
                (scheduler.DAILY_SCHEDULE_TRIGGER_LIMIT,),
            ),
        )

        assert run(scheduler._claim_next_schedule()) is None

    def test_missing_user_fails_the_task_even_when_it_recurs(self, env):
        schedule_id = seed_schedule(env.url, 999, unit="hour")

        poll(env)

        row = schedule_row(env.url, schedule_id)
        assert row["status"] == "failed" and row["error"] == "user not found"
        assert outcomes(env, schedule_id) == ["failed"]


class TestRecurringSchedules:
    def advanced_once(self, env, schedule_id, original_run_at):
        row = schedule_row(env.url, schedule_id)
        assert row["status"] == "pending"
        assert row["last_run_at"] == original_run_at
        assert row["run_at"] == scheduler._calculate_next_run_at(original_run_at, "hour", 1)
        assert row["claim_token"] is None and row["stage"] == "idle"
        return row

    @pytest.mark.parametrize("result", ["success", "model_error", "delivery_error"])
    def test_next_run_advances_exactly_once_whatever_the_result(self, env, result):
        seed_user(env.url, 1)
        schedule_id = seed_schedule(env.url, 1, unit="hour")
        original_run_at = schedule_row(env.url, schedule_id)["run_at"]
        if result == "model_error":

            async def explode(tool_context):
                raise RuntimeError("provider exploded")

            env.ai.hook = explode
        if result == "delivery_error":
            env.telegram.fail_with = RuntimeError("telegram timed out")

        poll(env)
        poll(env)  # 下一次运行时间在未来，第二轮不会再执行

        row = self.advanced_once(env, schedule_id, original_run_at)
        assert env.ai.calls == 1
        if result == "success":
            assert row["error"] is None and row["executed_at"] is not None
        else:
            expected = "provider exploded" if result == "model_error" else "telegram timed out"
            assert expected in row["error"]
        assert outcomes(env, schedule_id) == [
            "completed" if result == "success" else "failed"
        ]

    def test_one_shot_task_fails_when_the_model_errors(self, env):
        seed_user(env.url, 1)
        schedule_id = seed_schedule(env.url, 1)

        async def explode(tool_context):
            raise RuntimeError("provider exploded")

        env.ai.hook = explode
        poll(env)

        row = schedule_row(env.url, schedule_id)
        assert row["status"] == "failed" and "provider exploded" in row["error"]


class TestRetryBeforeSideEffects:
    def test_known_failure_before_side_effects_is_retried_then_given_up(self, env, monkeypatch):
        seed_user(env.url, 1)
        schedule_id = seed_schedule(env.url, 1)

        async def failing(user_id):
            raise RuntimeError("database hiccup while reading the user")

        monkeypatch.setattr(scheduler, "build_user_state_prompt", failing)

        for expected_attempts in (1, 2):
            poll(env)
            row = schedule_row(env.url, schedule_id)
            assert row["status"] == "pending" and row["claim_attempts"] == expected_attempts
            assert "database hiccup" in row["error"]
        poll(env)

        row = schedule_row(env.url, schedule_id)
        assert row["status"] == "failed"
        assert outcomes(env, schedule_id) == ["retry", "retry", "failed"]
        assert env.ai.calls == 0

    def test_a_failed_task_is_not_retried_within_the_same_poll(self, env, monkeypatch):
        seed_user(env.url, 1)
        schedule_id = seed_schedule(env.url, 1)

        async def failing(user_id):
            raise RuntimeError("database hiccup")

        monkeypatch.setattr(scheduler, "build_user_state_prompt", failing)
        poll(env)

        assert outcomes(env, schedule_id) == ["retry"]


class TestShutdown:
    def test_no_new_work_is_claimed_once_the_application_is_stopping(self, env):
        seed_user(env.url, 1)
        schedule_id = seed_schedule(env.url, 1)
        env.context.application = SimpleNamespace(running=False)

        poll(env)

        row = schedule_row(env.url, schedule_id)
        assert row["status"] == "pending" and row["claim_token"] is None
        assert attempts(env.url, "schedule", schedule_id) == []

    def test_a_claim_that_has_not_started_is_released_when_the_application_stops(
        self, env, monkeypatch
    ):
        seed_user(env.url, 1)
        schedule_id = seed_schedule(env.url, 1)
        application = SimpleNamespace(running=True)
        env.context.application = application
        original = scheduler.build_user_state_prompt

        async def stop_while_working(user_id):
            application.running = False
            return await original(user_id)

        monkeypatch.setattr(scheduler, "build_user_state_prompt", stop_while_working)
        poll(env)

        row = schedule_row(env.url, schedule_id)
        assert row["status"] == "pending" and row["claim_token"] is None
        assert row["claim_attempts"] == 0  # 没开始，不算一次尝试
        assert outcomes(env, schedule_id) == ["released"]
        assert env.ai.calls == 0

    def test_cancelling_a_waiting_worker_releases_its_claim(self, env):
        seed_user(env.url, 1)
        schedule_id = seed_schedule(env.url, 1)

        async def scenario():
            claim = await scheduler._claim_next_schedule()
            lock = scheduler.get_conversation_lock(1)
            async with lock:  # 用户的会话正在进行，worker 在等锁
                worker = asyncio.create_task(scheduler._process_schedule_task(claim, env.context))
                await asyncio.sleep(0.2)
                worker.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await worker

        run(scenario())

        row = schedule_row(env.url, schedule_id)
        assert row["status"] == "pending" and row["claim_token"] is None
        assert outcomes(env, schedule_id) == ["released"]

    def test_cancelling_a_running_worker_leaves_its_claim_to_expire(self, env):
        seed_user(env.url, 1)
        schedule_id = seed_schedule(env.url, 1)
        started = []

        async def hang(tool_context):
            started.append(True)
            await asyncio.Event().wait()

        env.ai.hook = hang

        async def scenario():
            claim = await scheduler._claim_next_schedule()
            worker = asyncio.create_task(scheduler._process_schedule_task(claim, env.context))
            while not started:
                await asyncio.sleep(0.05)
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker

        run(scenario())

        # 已经可能产生副作用：不释放，由下一次启动按结果未知回收。
        assert schedule_row(env.url, schedule_id)["stage"] == "generating"
        expire(env, schedule_id)
        poll(env)
        assert schedule_row(env.url, schedule_id)["status"] == "failed"
        assert outcomes(env, schedule_id) == ["unknown"]


class TestHousekeeping:
    def test_attempts_without_a_matching_task_row_are_closed(self, env):
        seed_user(env.url, 1)
        schedule_id = seed_schedule(env.url, 1)
        run(scheduler._claim_next_schedule())
        # schedule_ai_message_tool 复用旧行时会清掉 claim。
        execute(
            env.url,
            (
                "UPDATE ai_schedules SET status = 'pending', claim_token = NULL, "
                "claim_until = NULL, stage = 'idle' WHERE id = %s",
                (schedule_id,),
            ),
        )

        assert run(job_claims.sweep_orphaned_attempts(job_claims.SCHEDULE_JOB)) == 1
        assert outcomes(env, schedule_id) == ["superseded"]
        # 仍然挂在任务行上的尝试不会被关闭。
        run(scheduler._claim_next_schedule())
        assert run(job_claims.sweep_orphaned_attempts(job_claims.SCHEDULE_JOB)) == 0

    def test_old_finished_attempts_are_pruned(self, env):
        seed_user(env.url, 1)
        schedule_id = seed_schedule(env.url, 1)
        poll(env)
        execute(
            env.url,
            (
                "UPDATE ai_job_attempts SET finished_at = UTC_TIMESTAMP() - INTERVAL %s DAY",
                (job_claims.ATTEMPT_RETENTION_DAYS + 1,),
            ),
        )

        assert run(job_claims.prune_attempts()) == 1
        assert attempts(env.url, "schedule", schedule_id) == []

    def test_replacing_an_old_schedule_never_recycles_one_that_is_executing(self, env):
        seed_user(env.url, 1)
        executing = seed_schedule(env.url, 1)
        run(scheduler._claim_next_schedule())
        # 12 条上限：1 条执行中、2 条 pending、其余已执行。
        for _ in range(2):
            seed_schedule(env.url, 1, minutes_overdue=-60)
        executed = [seed_schedule(env.url, 1) for _ in range(9)]
        execute(
            env.url,
            (
                f"UPDATE ai_schedules SET status = 'executed' WHERE id IN ({', '.join(['%s'] * len(executed))})",
                tuple(executed),
            ),
        )

        schedule_id, _, replaced, blocked = run(
            schedule_tools._create_or_replace_schedule(
                1,
                fetch(env.url, "SELECT UTC_TIMESTAMP() + INTERVAL 1 DAY AS t")[0]["t"],
                "new reminder",
                None,
                "do something",
                "none",
                1,
            )
        )

        assert blocked is None and replaced is True
        assert schedule_id == executed[0]
        row = schedule_row(env.url, executing)
        assert row["status"] == "executing" and row["claim_token"] is not None
