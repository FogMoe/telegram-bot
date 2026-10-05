import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace

from fogmoe_telegram_bot.features.ai import schedule_limits, scheduler


def _claim(schedule_id, **overrides):
    values = dict(
        schedule_id=schedule_id,
        user_id=123,
        run_at=datetime(2026, 7, 29, 12, 0, 0),
        created_at=datetime(2026, 7, 29, 11, 0, 0),
        trigger_reason="scheduled reminder",
        context_text="",
        instruction="send reminder",
        recurrence_unit="none",
        recurrence_interval=1,
        token="a" * 32,
        attempt=1,
    )
    values.update(overrides)
    return scheduler.ScheduleClaim(**values)


def test_claim_next_schedule_skips_registered_users_without_coins(monkeypatch):
    captured_queries = []

    @asynccontextmanager
    async def fake_transaction():
        yield SimpleNamespace()

    async def fake_fetch_all(sql, params, **kwargs):
        captured_queries.append((sql, params))
        return []

    monkeypatch.setattr(scheduler.mysql_connection, "transaction", fake_transaction)
    monkeypatch.setattr(scheduler.mysql_connection, "fetch_all", fake_fetch_all)

    assert asyncio.run(scheduler._claim_next_schedule()) is None

    query, params = captured_queries[0]
    query = " ".join(query.split())
    assert "LEFT JOIN user AS u ON u.id = s.user_id" in query
    assert "COALESCE(u.coins, 0) + COALESCE(u.coins_paid, 0) > 0" in query
    assert "u.ai_schedule_trigger_date <> UTC_DATE()" in query
    assert "u.ai_schedule_trigger_count < %s" in query
    # 一次只 claim 一个：崩溃最多卡住当前这一个。
    assert "LIMIT 1" in query
    assert params == (24,)


def test_daily_schedule_trigger_reservation_uses_atomic_database_update(monkeypatch):
    executed = []

    async def fake_execute(sql, params):
        executed.append((sql, params))
        return 1

    monkeypatch.setattr(schedule_limits.mysql_connection, "execute", fake_execute)

    assert asyncio.run(schedule_limits.reserve_daily_schedule_trigger(123)) is True

    query, params = executed[0]
    query = " ".join(query.split())
    assert "ai_schedule_trigger_date = UTC_DATE()" in query
    assert "ai_schedule_trigger_count + 1" in query
    assert "ai_schedule_trigger_count < %s" in query
    assert params == (123, 24)


def test_daily_schedule_trigger_reservation_rejects_reached_limit(monkeypatch):
    async def fake_execute(*args, **kwargs):
        return 0

    monkeypatch.setattr(schedule_limits.mysql_connection, "execute", fake_execute)

    assert asyncio.run(schedule_limits.reserve_daily_schedule_trigger(123)) is False


def test_claimed_schedule_returns_to_pending_if_coins_are_exhausted(monkeypatch):
    released = []

    async def fake_user_state_prompt(user_id):
        assert user_id == 123
        return '<user_state coins="0" />'

    async def fake_user_coins(user_id):
        assert user_id == 123
        return 0

    async def fake_release_claim(run, outcome, *, attempts_expr, error=None):
        released.append((run.claim.schedule_id, outcome))

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("zero-coin schedule must not call AI or write history")

    monkeypatch.setattr(scheduler, "build_user_state_prompt", fake_user_state_prompt)
    monkeypatch.setattr(
        scheduler.process_user,
        "async_get_user_coins",
        fake_user_coins,
    )
    monkeypatch.setattr(scheduler, "_release_claim", fake_release_claim)
    monkeypatch.setattr(scheduler, "_begin_generation", fail_if_called)
    monkeypatch.setattr(
        scheduler.mysql_connection,
        "async_insert_chat_record",
        fail_if_called,
    )
    monkeypatch.setattr(scheduler.ai_chat, "get_ai_response", fail_if_called)

    run = scheduler._ScheduleRun(_claim(7))

    asyncio.run(scheduler._process_schedule_task_locked(run, SimpleNamespace()))

    assert released == [(7, "paused")]


def test_claimed_schedule_stops_at_daily_trigger_limit(monkeypatch):
    begun = []

    async def fake_user_state_prompt(user_id):
        assert user_id == 123
        return '<user_state coins="1" />'

    async def fake_user_coins(user_id):
        assert user_id == 123
        return 1

    async def fake_begin_generation(run):
        # 每日额度已满：事务里已经把任务放回 pending，这里返回 False。
        begun.append(run.claim.schedule_id)
        return False

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("daily-limited schedule must not call AI or write history")

    monkeypatch.setattr(scheduler, "build_user_state_prompt", fake_user_state_prompt)
    monkeypatch.setattr(
        scheduler.process_user,
        "async_get_user_coins",
        fake_user_coins,
    )
    monkeypatch.setattr(scheduler, "_begin_generation", fake_begin_generation)
    monkeypatch.setattr(
        scheduler.mysql_connection,
        "async_insert_chat_record",
        fail_if_called,
    )
    monkeypatch.setattr(scheduler.ai_chat, "get_ai_response", fail_if_called)

    run = scheduler._ScheduleRun(_claim(8))

    asyncio.run(scheduler._process_schedule_task_locked(run, SimpleNamespace()))

    assert begun == [8]
