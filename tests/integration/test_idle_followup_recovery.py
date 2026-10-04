"""空闲跟进的 claim 所有权、租约、阶段与崩溃恢复（真实 MySQL）。

约定见 docs/job-recovery.md。“崩溃”用 SimulatedCrash（BaseException）模拟。
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
    history_messages,
    idle_row,
    seed_history,
    seed_idle_followup,
    seed_user,
)
from mysql_support import execute, run

from features.ai import conversation_locks, idle_followup, job_claims
from features.ai.router import AI_SERVICE_ERROR_MESSAGE

REPLY = "just checking in"
USER_ID = 1


@pytest.fixture
def env(app_database, monkeypatch):
    telegram = FakeTelegram()
    ai = FakeAI(REPLY)
    recap = SimpleNamespace(calls=0, hook=None)

    async def fake_recap(user_id, dialogue, memory_context):
        recap.calls += 1
        if recap.hook is not None:
            await recap.hook()
        return {
            "recap": "we talked about water",
            "open_loops": "",
            "suggested_follow_up": "ask how it went",
            "memory_suggestion": {"impression": "", "diary": ""},
        }

    async def no_media(**kwargs):
        return []

    monkeypatch.setattr(idle_followup, "_generate_recap", fake_recap)
    monkeypatch.setattr(idle_followup.ai_chat, "get_ai_response", ai.get_ai_response)
    monkeypatch.setattr(idle_followup, "send_ai_reply_with_stickers", telegram.send_reply)
    monkeypatch.setattr(idle_followup, "send_generated_media", no_media)
    monkeypatch.setattr(idle_followup.summary, "schedule_summary_generation", lambda *a, **k: None)
    monkeypatch.setattr(idle_followup, "IDLE_FOLLOWUP_HEARTBEAT_SECONDS", 3600)
    # asyncio.Lock 一旦发生过竞争就绑定了那个事件循环；每个测试用自己的会话锁表。
    monkeypatch.setattr(conversation_locks, "_CONVERSATION_LOCKS", {})

    seed_user(app_database, USER_ID)
    seed_history(app_database, USER_ID)
    seed_idle_followup(app_database, USER_ID)
    return SimpleNamespace(
        url=app_database,
        ai=ai,
        telegram=telegram,
        recap=recap,
        context=SimpleNamespace(bot=FakeBot()),
    )


def poll(env):
    run(idle_followup.run_idle_followup_job(env.context))


def expire(env):
    expire_lease(env.url, "ai_idle_followups", "user_id", USER_ID)


def outcomes(env):
    return [row["outcome"] for row in attempts(env.url, "idle_followup", USER_ID)]


def recap_events(env):
    return [
        message
        for message in history_messages(env.url, USER_ID)
        if isinstance(message.get("content"), str) and 'origin="idle_recap"' in message["content"]
    ]


SNAPSHOT_SQL = (
    "SELECT status, stage, activity_version, retry_count, last_error, last_fired_at, "
    "claim_token, claim_attempts, next_run_at FROM ai_idle_followups WHERE user_id = %s"
)


async def snapshot():
    return tuple((await a_fetch(SNAPSHOT_SQL, (USER_ID,)))[0])


async def expire_in_loop():
    await a_execute(
        "UPDATE ai_idle_followups SET claim_until = UTC_TIMESTAMP() - INTERVAL 5 SECOND "
        "WHERE user_id = %s",
        (USER_ID,),
    )


class TestCrashRecovery:
    def test_crash_before_side_effects_is_reclaimed_with_a_new_token_and_runs_once(self, env):
        state = {"crash": True}

        async def die():
            if state["crash"]:
                raise SimulatedCrash("process died while generating the recap")

        env.recap.hook = die
        with pytest.raises(SimulatedCrash):
            poll(env)

        crashed = idle_row(env.url, USER_ID)
        assert crashed["status"] == "executing" and crashed["stage"] == "claimed"
        first_token = crashed["claim_token"]
        assert len(first_token) == 32

        state["crash"] = False
        poll(env)  # 租约还没到期
        assert env.ai.calls == 0 and idle_row(env.url, USER_ID)["claim_token"] == first_token

        expire(env)
        poll(env)

        done = idle_row(env.url, USER_ID)
        assert done["status"] == "fired"
        assert done["claim_token"] is None and done["stage"] == "idle"
        assert done["last_error"] is None
        assert done["activity_version"] == crashed["activity_version"]
        assert env.ai.calls == 1
        assert env.telegram.delivered == [REPLY]
        rows = attempts(env.url, "idle_followup", USER_ID)
        assert [row["outcome"] for row in rows] == ["expired", "completed"]
        assert rows[0]["claim_token"] == first_token and rows[1]["claim_token"] != first_token
        assert [row["job_version"] for row in rows] == [1, 1]
        assert len(recap_events(env)) == 1

    def test_crash_after_telegram_accepted_the_followup_is_not_resent(self, env):
        """外部操作成功、本地结果还没记录：按结果未知处理，不重发、不重跑。"""
        env.telegram.crash_after_send = True
        with pytest.raises(SimulatedCrash):
            poll(env)

        crashed = idle_row(env.url, USER_ID)
        assert crashed["status"] == "executing" and crashed["stage"] == "delivering"
        assert env.telegram.delivered == [REPLY]

        env.telegram.crash_after_send = False
        expire(env)
        poll(env)
        poll(env)

        after = idle_row(env.url, USER_ID)
        assert after["status"] == "fired"
        assert "outcome is unknown" in after["last_error"]
        assert after["claim_token"] is None
        assert env.ai.calls == 1 and env.recap.calls == 1
        assert env.telegram.delivered == [REPLY]
        assert outcomes(env) == ["unknown"]
        assert attempts(env.url, "idle_followup", USER_ID)[0]["stage"] == "delivering"
        # 这一轮的历史只写了一次。
        assert len(recap_events(env)) == 1

    def test_crash_during_generation_is_not_replayed(self, env):
        async def die(tool_context):
            raise SimulatedCrash("process died while tools were running")

        env.ai.hook = die
        with pytest.raises(SimulatedCrash):
            poll(env)
        assert idle_row(env.url, USER_ID)["stage"] == "generating"

        env.ai.hook = None
        expire(env)
        poll(env)

        after = idle_row(env.url, USER_ID)
        assert after["status"] == "fired" and "Interrupted during generating" in after["last_error"]
        assert env.ai.calls == 1 and env.telegram.delivered == []
        assert outcomes(env) == ["unknown"]

    def test_a_followup_that_keeps_crashing_before_it_starts_is_abandoned(self, env):
        async def die():
            raise SimulatedCrash("process died while generating the recap")

        env.recap.hook = die
        for _ in range(idle_followup.IDLE_FOLLOWUP_MAX_CLAIM_ATTEMPTS):
            with pytest.raises(SimulatedCrash):
                poll(env)
            expire(env)
        poll(env)

        after = idle_row(env.url, USER_ID)
        assert after["status"] == "fired" and "Abandoned after 5 claims" in after["last_error"]
        assert outcomes(env) == ["expired"] * 4 + ["abandoned"]

    def test_legacy_executing_row_without_a_token_is_closed_as_unknown(self, env):
        # 迁移之前的版本留下的 executing 行：没有 token，租约已过期。
        execute(
            env.url,
            (
                "UPDATE ai_idle_followups SET status = 'executing', stage = 'generating', "
                "claim_until = UTC_TIMESTAMP() - INTERVAL 1 MINUTE WHERE user_id = %s",
                (USER_ID,),
            ),
        )

        poll(env)

        after = idle_row(env.url, USER_ID)
        assert after["status"] == "fired" and "outcome is unknown" in after["last_error"]
        assert env.ai.calls == 0


class TestStaleWorker:
    @pytest.mark.parametrize("with_tools", [False, True])
    def test_a_stale_worker_that_finishes_late_is_rejected(self, env, with_tools):
        """租约到期并被回收后，旧 worker 迟到完成：不投递、不写状态。"""
        if with_tools:
            env.ai.tool_logs = [
                {
                    "type": "tool_result",
                    "tool_name": "read_doc",
                    "arguments": {"topic": "memory"},
                    "result": {"content": "memory details"},
                    "tool_call_id": "call_1",
                }
            ]
        snapshots = {}

        async def scenario():
            started, release = asyncio.Event(), asyncio.Event()

            async def slow(tool_context):
                started.set()
                await release.wait()

            env.ai.hook = slow
            claims = await idle_followup._claim_due_followups()
            worker = asyncio.create_task(idle_followup._process_claim(claims[0], env.context))
            await started.wait()

            await expire_in_loop()
            assert await idle_followup._recover_expired_followups() == 1
            snapshots["after_recovery"] = await snapshot()
            release.set()
            await worker
            snapshots["after_late_worker"] = await snapshot()

        run(scenario())

        assert snapshots["after_late_worker"] == snapshots["after_recovery"]
        assert env.telegram.delivered == []
        assert outcomes(env) == ["unknown"]
        # 已经执行过的工具仍然写进历史，但没有发送给用户。
        assert len(recap_events(env)) == (1 if with_tools else 0)

    def test_a_stale_worker_that_was_requeued_cannot_deliver_a_second_time(
        self, env, monkeypatch
    ):
        snapshots = {}

        async def scenario():
            started, release = asyncio.Event(), asyncio.Event()

            async def stall_first():
                if env.recap.calls == 1:
                    started.set()
                    await release.wait()

            env.recap.hook = stall_first
            claims_a = await idle_followup._claim_due_followups()
            worker_a = asyncio.create_task(idle_followup._process_claim(claims_a[0], env.context))
            await started.wait()

            # 另一个进程：租约到期、回收、用新 token 重新执行。每个进程有自己的会话锁。
            monkeypatch.setattr(idle_followup, "get_conversation_lock", lambda _id: asyncio.Lock())
            await expire_in_loop()
            assert await idle_followup._recover_expired_followups() == 1
            claims_b = await idle_followup._claim_due_followups()
            assert claims_b[0].token != claims_a[0].token
            assert claims_b[0].activity_version == claims_a[0].activity_version
            await idle_followup._process_claim(claims_b[0], env.context)
            snapshots["after_b"] = await snapshot()

            release.set()
            await worker_a
            snapshots["after_a"] = await snapshot()

        run(scenario())

        assert snapshots["after_a"] == snapshots["after_b"]
        assert env.ai.calls == 1
        assert env.telegram.delivered == [REPLY]
        assert outcomes(env) == ["expired", "completed"]

    def test_old_claim_cannot_write_after_the_followup_was_reclaimed(self, env):
        async def scenario():
            (old,) = await idle_followup._claim_due_followups()
            await expire_in_loop()
            await idle_followup._recover_expired_followups()
            (new,) = await idle_followup._claim_due_followups()
            assert new.token != old.token

            old_run = idle_followup._IdleRun(old)
            assert not await idle_followup._claim_is_current(old)
            assert await idle_followup._claim_is_current(new)
            with pytest.raises(job_claims.ClaimLostError):
                await idle_followup._enter_stage(old_run, "generating")
            with pytest.raises(job_claims.ClaimLostError):
                await idle_followup._mark_claim_fired(old)
            with pytest.raises(job_claims.ClaimLostError):
                await idle_followup._record_claim_failure(old_run, RuntimeError("late"))
            with pytest.raises(job_claims.ClaimLostError):
                await idle_followup._pause_claim_until_coins_available(old)
            assert not await job_claims.renew_lease(
                job_claims.IDLE_FOLLOWUP_JOB, USER_ID, old.token, 60,
                extra_where=" AND activity_version = %s", extra_params=(old.activity_version,),
            )

        run(scenario())

        row = idle_row(env.url, USER_ID)
        assert row["status"] == "executing" and row["stage"] == "claimed"
        assert row["claim_attempts"] == 2


class TestUserActivity:
    def test_new_user_activity_invalidates_the_claim(self, env):
        async def scenario():
            started, release = asyncio.Event(), asyncio.Event()

            async def stall():
                started.set()
                await release.wait()

            env.recap.hook = stall
            claims = await idle_followup._claim_due_followups()
            worker = asyncio.create_task(idle_followup._process_claim(claims[0], env.context))
            await started.wait()
            await idle_followup.note_incoming_private_message(USER_ID)
            release.set()
            await worker

        run(scenario())

        row = idle_row(env.url, USER_ID)
        assert row["activity_version"] == 2 and row["status"] == "fired"
        assert row["claim_token"] is None
        assert env.ai.calls == 0 and env.telegram.delivered == []
        # 旧 worker 悄悄退出；留下的未完成尝试由清扫关闭。
        assert outcomes(env) == [None]
        assert run(job_claims.sweep_orphaned_attempts(job_claims.IDLE_FOLLOWUP_JOB)) == 1
        assert outcomes(env) == ["superseded"]

    def test_version_change_rejects_a_claim_even_when_the_token_is_unchanged(self, env):
        async def scenario():
            (claim,) = await idle_followup._claim_due_followups()
            # 用户有了新活动，而 token 还留在行上（例如别的代码路径只改了 version）。
            await a_execute(
                "UPDATE ai_idle_followups SET activity_version = activity_version + 1 "
                "WHERE user_id = %s",
                (USER_ID,),
            )
            assert not await idle_followup._claim_is_current(claim)
            with pytest.raises(job_claims.ClaimLostError):
                await idle_followup._enter_stage(idle_followup._IdleRun(claim), "generating")
            with pytest.raises(job_claims.ClaimLostError):
                await idle_followup._mark_claim_fired(claim)

        run(scenario())

        row = idle_row(env.url, USER_ID)
        assert row["status"] == "executing" and row["activity_version"] == 2

    def test_a_new_turn_re_arms_the_followup_and_rejects_the_old_claim(self, env):
        async def scenario():
            (claim,) = await idle_followup._claim_due_followups()
            await idle_followup.arm_from_private_turn(USER_ID)
            with pytest.raises(job_claims.ClaimLostError):
                await idle_followup._mark_claim_fired(claim)

        run(scenario())

        row = idle_row(env.url, USER_ID)
        assert row["status"] == "armed" and row["activity_version"] == 2
        assert row["claim_token"] is None and row["stage"] == "idle"

    def test_heartbeat_abandons_a_superseded_run_so_the_user_is_not_blocked(
        self, env, monkeypatch
    ):
        monkeypatch.setattr(idle_followup, "IDLE_FOLLOWUP_HEARTBEAT_SECONDS", 0.2)
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
            claims = await idle_followup._claim_due_followups()
            worker = asyncio.create_task(idle_followup._process_claim(claims[0], env.context))
            while "abort_event" not in observed:
                await asyncio.sleep(0.05)
            await idle_followup.note_incoming_private_message(USER_ID)
            await asyncio.wait_for(worker, timeout=5)
            observed["lock_free"] = not idle_followup.get_conversation_lock(USER_ID).locked()

        run(scenario())

        assert observed["cancelled"] and observed["abort_event"].is_set()
        assert observed["lock_free"]
        assert env.telegram.delivered == []


class TestLease:
    def test_heartbeat_keeps_a_long_running_claim_alive(self, env, monkeypatch):
        monkeypatch.setattr(idle_followup, "IDLE_FOLLOWUP_LEASE_SECONDS", 2)
        monkeypatch.setattr(idle_followup, "IDLE_FOLLOWUP_HEARTBEAT_SECONDS", 0.3)
        recovered_while_running = []

        async def slow(tool_context):
            await asyncio.sleep(3.2)

        env.ai.hook = slow

        async def scenario():
            claims = await idle_followup._claim_due_followups()
            worker = asyncio.create_task(idle_followup._process_claim(claims[0], env.context))
            await asyncio.sleep(2.6)
            recovered_while_running.append(await idle_followup._recover_expired_followups())
            await worker

        run(scenario())

        assert recovered_while_running == [0]
        assert idle_row(env.url, USER_ID)["status"] == "fired"
        assert env.telegram.delivered == [REPLY]
        assert outcomes(env) == ["completed"]

    def test_execution_limit_cancels_a_stuck_worker(self, env, monkeypatch):
        monkeypatch.setattr(idle_followup, "IDLE_FOLLOWUP_EXECUTION_TIMEOUT_SECONDS", 0.5)

        async def hang(tool_context):
            await asyncio.Event().wait()

        env.ai.hook = hang
        poll(env)

        row = idle_row(env.url, USER_ID)
        # 主模型已经开始：不能再安全重试，直接结束并留下原因。
        assert row["status"] == "fired" and "TimeoutError" in row["last_error"]
        assert outcomes(env) == ["failed"]
        assert env.telegram.delivered == []


class TestRetryPolicy:
    def test_failure_before_side_effects_is_retried_later(self, env):
        async def explode():
            raise RuntimeError("recap provider is down")

        env.recap.hook = explode
        poll(env)

        row = idle_row(env.url, USER_ID)
        assert row["status"] == "armed" and row["retry_count"] == 1
        assert "recap provider is down" in row["last_error"]
        assert row["claim_token"] is None
        assert outcomes(env) == ["retry"]
        assert env.ai.calls == 0
        retry_in_future = run(
            a_fetch(
                "SELECT next_run_at > UTC_TIMESTAMP() FROM ai_idle_followups WHERE user_id = %s",
                (USER_ID,),
            )
        )
        assert retry_in_future[0][0] == 1

    def test_main_model_failure_without_tool_effects_is_retried(self, env):
        env.ai.reply = AI_SERVICE_ERROR_MESSAGE
        poll(env)

        row = idle_row(env.url, USER_ID)
        assert row["status"] == "armed" and row["retry_count"] == 1
        assert outcomes(env) == ["retry"]
        assert env.telegram.delivered == []

    def test_failure_after_tools_may_have_run_is_not_retried(self, env, monkeypatch):
        async def failing_persist(*args, **kwargs):
            raise RuntimeError("database went away while saving the turn")

        monkeypatch.setattr(idle_followup, "_persist_completed_turn", failing_persist)
        poll(env)

        row = idle_row(env.url, USER_ID)
        assert row["status"] == "fired" and "database went away" in row["last_error"]
        assert outcomes(env) == ["failed"]
        assert env.ai.calls == 1 and env.telegram.delivered == []

    def test_retries_are_bounded(self, env):
        execute(
            env.url,
            (
                "UPDATE ai_idle_followups SET retry_count = %s WHERE user_id = %s",
                (idle_followup.IDLE_FOLLOWUP_MAX_RETRIES - 1, USER_ID),
            ),
        )

        async def explode():
            raise RuntimeError("recap provider is down")

        env.recap.hook = explode
        poll(env)

        row = idle_row(env.url, USER_ID)
        assert row["status"] == "fired" and row["retry_count"] == idle_followup.IDLE_FOLLOWUP_MAX_RETRIES
        assert outcomes(env) == ["failed"]

    def test_delivery_errors_are_recorded_without_losing_the_local_result(self, env):
        env.telegram.fail_with = RuntimeError("telegram timed out")
        poll(env)

        row = idle_row(env.url, USER_ID)
        assert row["status"] == "fired" and "telegram timed out" in row["last_error"]
        rows = attempts(env.url, "idle_followup", USER_ID)
        assert [r["outcome"] for r in rows] == ["completed"]
        assert "telegram timed out" in rows[0]["error"]
        assert len(recap_events(env)) == 1


class TestClaimingAndShutdown:
    def test_a_claimed_followup_is_not_claimed_twice(self, env):
        async def scenario():
            first = await idle_followup._claim_due_followups()
            second = await idle_followup._claim_due_followups()
            return first, second

        first, second = run(scenario())

        assert len(first) == 1 and second == []

    def test_concurrent_pollers_never_claim_the_same_followup(self, env):
        async def scenario():
            return await asyncio.gather(
                idle_followup._claim_due_followups(),
                idle_followup._claim_due_followups(),
                idle_followup._claim_due_followups(),
            )

        claimed = [claim for batch in run(scenario()) for claim in batch]

        assert len(claimed) == 1
        assert idle_row(env.url, USER_ID)["claim_token"] == claimed[0].token

    def test_users_without_coins_are_not_claimed(self, env):
        execute(env.url, ("UPDATE `user` SET coins = 0, coins_paid = 0 WHERE id = %s", (USER_ID,)))

        assert run(idle_followup._claim_due_followups()) == []

    def test_coins_exhausted_after_claiming_returns_the_followup_to_armed(self, env):
        async def scenario():
            (claim,) = await idle_followup._claim_due_followups()
            await a_execute("UPDATE `user` SET coins = 0, coins_paid = 0 WHERE id = %s", (USER_ID,))
            await idle_followup._process_claim(claim, env.context)

        run(scenario())

        row = idle_row(env.url, USER_ID)
        assert row["status"] == "armed" and row["claim_attempts"] == 0
        assert outcomes(env) == ["paused"]
        assert env.ai.calls == 0

    def test_no_new_work_is_claimed_once_the_application_is_stopping(self, env):
        env.context.application = SimpleNamespace(running=False)

        poll(env)

        row = idle_row(env.url, USER_ID)
        assert row["status"] == "armed" and row["claim_token"] is None
        assert attempts(env.url, "idle_followup", USER_ID) == []

    def test_a_claim_that_has_not_started_is_released_when_the_application_stops(self, env):
        async def scenario():
            (claim,) = await idle_followup._claim_due_followups()
            env.context.application = SimpleNamespace(running=False)
            await idle_followup._process_claim(claim, env.context)

        run(scenario())

        row = idle_row(env.url, USER_ID)
        assert row["status"] == "armed" and row["claim_token"] is None
        assert row["claim_attempts"] == 0
        assert outcomes(env) == ["released"]
        assert env.ai.calls == 0 and env.recap.calls == 0

    def test_cancelling_a_waiting_worker_releases_its_claim(self, env):
        async def scenario():
            (claim,) = await idle_followup._claim_due_followups()
            async with idle_followup.get_conversation_lock(USER_ID):
                worker = asyncio.create_task(idle_followup._process_claim(claim, env.context))
                await asyncio.sleep(0.2)
                worker.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await worker

        run(scenario())

        row = idle_row(env.url, USER_ID)
        assert row["status"] == "armed" and row["claim_token"] is None
        assert outcomes(env) == ["released"]

    def test_attempts_of_a_deleted_followup_are_closed(self, env):
        run(idle_followup._claim_due_followups())
        run(idle_followup.cancel_idle_followup(USER_ID))

        assert run(job_claims.sweep_orphaned_attempts(job_claims.IDLE_FOLLOWUP_JOB)) == 1
        assert outcomes(env) == ["superseded"]
