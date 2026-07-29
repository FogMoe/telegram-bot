import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace

from features.ai import schedule_limits, scheduler


def test_claim_due_schedules_skips_registered_users_without_coins(monkeypatch):
    captured_queries = []

    @asynccontextmanager
    async def fake_transaction():
        yield SimpleNamespace()

    async def fake_fetch_all(sql, params, **kwargs):
        captured_queries.append((sql, params))
        return []

    monkeypatch.setattr(scheduler.mysql_connection, "transaction", fake_transaction)
    monkeypatch.setattr(scheduler.mysql_connection, "fetch_all", fake_fetch_all)

    assert asyncio.run(scheduler._claim_due_schedules()) == []

    query, params = captured_queries[0]
    query = " ".join(query.split())
    assert "LEFT JOIN user AS u ON u.id = s.user_id" in query
    assert "COALESCE(u.coins, 0) + COALESCE(u.coins_paid, 0) > 0" in query
    assert "u.ai_schedule_trigger_date <> UTC_DATE()" in query
    assert "u.ai_schedule_trigger_count < %s" in query
    assert params == (24, scheduler.SCHEDULE_BATCH_SIZE)


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
    status_updates = []

    async def fake_user_state_prompt(user_id):
        assert user_id == 123
        return '<user_state coins="0" />'

    async def fake_user_coins(user_id):
        assert user_id == 123
        return 0

    async def fake_mark_schedule_status(schedule_id, status, *, error=None):
        status_updates.append((schedule_id, status, error))

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("zero-coin schedule must not call AI or write history")

    monkeypatch.setattr(scheduler, "build_user_state_prompt", fake_user_state_prompt)
    monkeypatch.setattr(
        scheduler.process_user,
        "async_get_user_coins",
        fake_user_coins,
    )
    monkeypatch.setattr(scheduler, "_mark_schedule_status", fake_mark_schedule_status)
    monkeypatch.setattr(scheduler, "reserve_daily_schedule_trigger", fail_if_called)
    monkeypatch.setattr(
        scheduler.mysql_connection,
        "async_insert_chat_record",
        fail_if_called,
    )
    monkeypatch.setattr(scheduler.ai_chat, "get_ai_response", fail_if_called)

    task_row = (
        7,
        123,
        datetime(2026, 7, 29, 12, 0, 0),
        datetime(2026, 7, 29, 11, 0, 0),
        "scheduled reminder",
        "",
        "send reminder",
        "none",
        1,
    )

    asyncio.run(
        scheduler._process_schedule_task_locked(
            task_row,
            SimpleNamespace(),
        )
    )

    assert status_updates == [(7, "pending", None)]


def test_claimed_schedule_returns_to_pending_at_daily_trigger_limit(monkeypatch):
    status_updates = []

    async def fake_user_state_prompt(user_id):
        assert user_id == 123
        return '<user_state coins="1" />'

    async def fake_user_coins(user_id):
        assert user_id == 123
        return 1

    async def fake_reserve_daily_trigger(user_id):
        assert user_id == 123
        return False

    async def fake_mark_schedule_status(schedule_id, status, *, error=None):
        status_updates.append((schedule_id, status, error))

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("daily-limited schedule must not call AI or write history")

    monkeypatch.setattr(scheduler, "build_user_state_prompt", fake_user_state_prompt)
    monkeypatch.setattr(
        scheduler.process_user,
        "async_get_user_coins",
        fake_user_coins,
    )
    monkeypatch.setattr(
        scheduler,
        "reserve_daily_schedule_trigger",
        fake_reserve_daily_trigger,
    )
    monkeypatch.setattr(scheduler, "_mark_schedule_status", fake_mark_schedule_status)
    monkeypatch.setattr(
        scheduler.mysql_connection,
        "async_insert_chat_record",
        fail_if_called,
    )
    monkeypatch.setattr(scheduler.ai_chat, "get_ai_response", fail_if_called)

    task_row = (
        8,
        123,
        datetime(2026, 7, 29, 12, 0, 0),
        datetime(2026, 7, 29, 11, 0, 0),
        "scheduled reminder",
        "",
        "send reminder",
        "none",
        1,
    )

    asyncio.run(
        scheduler._process_schedule_task_locked(
            task_row,
            SimpleNamespace(),
        )
    )

    assert status_updates == [(8, "pending", None)]
