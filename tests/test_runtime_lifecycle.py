"""运行时的启动与关停顺序：准入、后台任务、HTTP 客户端、线程适配器、数据库引擎。"""

import asyncio
import time

import pytest
from telegram.ext import Application

from fogmoe_telegram_bot.app import bot_app, runtime_lifecycle
from fogmoe_telegram_bot.core import (
    admission,
    background,
    blocking,
    config,
    db,
    http_sessions,
    metrics,
)
from fogmoe_telegram_bot.core.admission import AdmissionSettings, Overloaded, OverloadReason
from fogmoe_telegram_bot.core.deadline import REASON_SHUTDOWN, Deadline, DeadlineExceeded
from fogmoe_telegram_bot.core.telegram_history import HistoryTrackingExtBot


@pytest.fixture(autouse=True)
def clean_runtime():
    def reset():
        admission.reset_admission()
        background.BACKGROUND.reopen()
        blocking.reopen_all()
        metrics.REGISTRY.reset()

    reset()
    yield
    reset()


@pytest.fixture
def recorded(monkeypatch):
    """把每一步关停动作换成记录顺序的替身。"""
    order: list[str] = []

    async def flush_history():
        order.append("flush telegram history")

    async def close_litellm():
        order.append("close litellm clients")

    async def dispose_engine():
        order.append("dispose database engine")

    monkeypatch.setattr(runtime_lifecycle, "flush_all_pending_events", flush_history)
    monkeypatch.setattr(runtime_lifecycle.litellm_client, "close_clients", close_litellm)
    monkeypatch.setattr(db, "dispose_engine", dispose_engine)
    return order


def test_shutdown_releases_every_resource_in_the_documented_order(recorded):
    class Session:
        closed = False

        def close(self):
            Session.closed = True
            recorded.append("close requests sessions")

    http_sessions.close_tracked_sessions()
    session = http_sessions.track_session(Session())  # 弱引用登记：调用方（线程本地）持有会话

    async def scenario():
        async def pending_summary():
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                recorded.append("cancel background tasks")
                raise

        background.spawn(pending_summary(), name="summary-1")
        await asyncio.sleep(0.01)
        # 线程适配器先用一次，让线程池真的存在。
        await blocking.tools().run(lambda: recorded.append("tool thread was used"))
        await runtime_lifecycle.shutdown_runtime()

    asyncio.run(scenario())

    assert recorded == [
        "tool thread was used",
        "cancel background tasks",
        "flush telegram history",
        "close litellm clients",
        "close requests sessions",
        "dispose database engine",
    ]
    assert admission.get_admission().closed  # 1. 停止接收新工作
    assert background.BACKGROUND.pending == 0 and background.BACKGROUND.closed
    assert blocking.tools().closed  # 5. 线程适配器已关闭
    assert Session.closed and session is not None


def test_a_failing_step_is_logged_and_the_remaining_steps_still_run(recorded, monkeypatch, caplog):
    async def broken_flush():
        raise RuntimeError("database is gone")

    monkeypatch.setattr(runtime_lifecycle, "flush_all_pending_events", broken_flush)

    with caplog.at_level("ERROR"):
        asyncio.run(runtime_lifecycle.shutdown_runtime())

    assert "shutdown step failed: flush telegram history" in caplog.text
    assert recorded == ["close litellm clients", "dispose database engine"]


def test_litellm_clients_are_closed_through_litellm(monkeypatch):
    from fogmoe_telegram_bot.features.ai import litellm_client

    closed = []

    async def fake_close():
        closed.append(True)

    monkeypatch.setattr(litellm_client.litellm, "close_litellm_async_clients", fake_close)

    asyncio.run(litellm_client.close_clients())

    assert closed == [True]


def test_a_litellm_close_failure_never_breaks_shutdown(monkeypatch):
    from fogmoe_telegram_bot.features.ai import litellm_client

    async def broken():
        raise RuntimeError("client already closed")

    monkeypatch.setattr(litellm_client.litellm, "close_litellm_async_clients", broken)

    asyncio.run(litellm_client.close_clients())


def test_the_database_engine_is_disposed_and_recreated_on_next_use(monkeypatch):
    disposed = []

    class FakeEngine:
        async def dispose(self):
            disposed.append(True)

    monkeypatch.setattr(db, "_ENGINE", FakeEngine())

    asyncio.run(db.dispose_engine())
    asyncio.run(db.dispose_engine())  # 重复调用无害

    assert disposed == [True]
    assert db._ENGINE is None


def test_begin_shutdown_closes_admission_rejects_the_queue_and_expires_in_flight_turns(
    settings_override,
):
    settings_override(
        CHAT_MAX_CONCURRENT_TURNS=1,
        CHAT_QUEUE_MAX_WAIT_SECONDS=30,
        RUNTIME_SHUTDOWN_GRACE_SECONDS=0,
    )

    async def scenario():
        controller = admission.get_admission()
        deadline = Deadline(60)
        outcome = {}

        async def in_flight():
            async with controller.slot(deadline=deadline):
                try:
                    async with deadline.guard():
                        await asyncio.sleep(30)
                except DeadlineExceeded as exc:
                    outcome["in_flight"] = exc.reason

        async def queued():
            try:
                async with controller.slot():
                    outcome["queued"] = "started"
            except Overloaded as exc:
                outcome["queued"] = exc.reason

        running = asyncio.create_task(in_flight())
        await asyncio.sleep(0.02)
        waiting = asyncio.create_task(queued())
        await asyncio.sleep(0.02)

        started = time.monotonic()
        runtime_lifecycle.begin_shutdown()
        runtime_lifecycle.begin_shutdown()  # 重复调用只生效一次
        await asyncio.gather(running, waiting)
        outcome["elapsed"] = time.monotonic() - started
        await controller.aclose()
        return outcome

    outcome = asyncio.run(scenario())

    assert outcome["queued"] is OverloadReason.SHUTTING_DOWN  # 排队的立刻被拒绝，还没扣费
    assert outcome["in_flight"] == REASON_SHUTDOWN  # 在途的轮次被取消并提示
    assert outcome["elapsed"] < 1.0


def test_start_runtime_reopens_the_runtime_and_starts_the_metrics_reporter(settings_override):
    settings_override(RUNTIME_METRICS_LOG_INTERVAL_SECONDS=60)

    async def scenario():
        admission.get_admission().close()
        background.BACKGROUND.closed or await background.BACKGROUND.shutdown()
        blocking.shutdown_all()

        runtime_lifecycle.start_runtime()

        names = {task.get_name() for task in asyncio.all_tasks()}
        state = (
            admission.get_admission().closed,
            background.BACKGROUND.closed,
            blocking.tools().closed,
        )
        await background.BACKGROUND.shutdown()
        return names, state

    names, state = asyncio.run(scenario())

    assert "metrics-reporter" in names
    assert state == (False, False, False)


def test_a_zero_interval_turns_the_metrics_reporter_off(settings_override):
    settings_override(RUNTIME_METRICS_LOG_INTERVAL_SECONDS=0)

    async def scenario():
        runtime_lifecycle.start_runtime()
        return background.BACKGROUND.pending

    assert asyncio.run(scenario()) == 0


@pytest.mark.slow
def test_the_application_notifies_the_runtime_before_ptb_starts_stopping(monkeypatch):
    order = []

    async def fake_stop(self):
        order.append("ptb stop")

    monkeypatch.setattr(Application, "stop", fake_stop)
    monkeypatch.setattr(runtime_lifecycle, "begin_shutdown", lambda: order.append("begin shutdown"))

    application = bot_app.create_application(
        config.AppSettings.from_values(TELEGRAM_BOT_TOKEN="123:abc"),
        bot=HistoryTrackingExtBot(token="123:abc"),
    )
    asyncio.run(application.stop())

    assert order == ["begin shutdown", "ptb stop"]
    assert isinstance(application, bot_app.BotApplication)


@pytest.mark.slow
def test_concurrent_updates_are_bounded_by_the_configured_value():
    application = bot_app.create_application(
        config.AppSettings.from_values(
            TELEGRAM_BOT_TOKEN="123:abc", TELEGRAM_CONCURRENT_UPDATES=48
        ),
        bot=HistoryTrackingExtBot(token="123:abc"),
    )

    assert application.concurrent_updates == 48


def test_the_default_concurrent_updates_leave_headroom_above_the_turn_capacity():
    settings = AdmissionSettings()
    defaults = config.AppSettings.from_values()

    assert defaults.TELEGRAM_CONCURRENT_UPDATES >= defaults.CHAT_MAX_CONCURRENT_TURNS + defaults.CHAT_MAX_QUEUED_TURNS
    assert settings.max_concurrent == defaults.CHAT_MAX_CONCURRENT_TURNS
    assert defaults.TELEGRAM_CONCURRENT_UPDATES < 256  # 比原来的无界 256 更小、有依据
