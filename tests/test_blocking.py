"""有界线程适配器、后台任务登记与同步 HTTP 会话登记。"""

import asyncio
import contextvars
import logging
import threading
import time

import pytest

from core import background, blocking, http_sessions, metrics


@pytest.fixture(autouse=True)
def fresh_metrics():
    metrics.REGISTRY.reset()
    yield
    metrics.REGISTRY.reset()


def make_adapter(workers=2):
    return blocking.BoundedThreadAdapter("test", lambda: workers)


def test_a_sync_call_runs_in_a_worker_thread_not_in_the_event_loop():
    adapter = make_adapter()

    async def scenario():
        loop_thread = threading.get_ident()
        worker_thread = await adapter.run(threading.get_ident)
        return loop_thread, worker_thread

    try:
        loop_thread, worker_thread = asyncio.run(scenario())
    finally:
        adapter.shutdown()

    assert worker_thread != loop_thread


def test_concurrency_is_bounded_by_the_pool_size_and_the_rest_queue():
    adapter = make_adapter(workers=2)
    lock = threading.Lock()
    state = {"running": 0, "peak": 0}

    def work():
        with lock:
            state["running"] += 1
            state["peak"] = max(state["peak"], state["running"])
        time.sleep(0.05)
        with lock:
            state["running"] -= 1

    async def scenario():
        calls = [asyncio.create_task(adapter.run(work)) for _ in range(6)]
        await asyncio.sleep(0.02)
        queued_midway = adapter.queued
        await asyncio.gather(*calls)
        return queued_midway

    try:
        queued_midway = asyncio.run(scenario())
    finally:
        adapter.shutdown()

    assert state["peak"] == 2
    assert queued_midway == 4
    snapshot = metrics.snapshot()
    waits = snapshot.histogram("blocking.wait_seconds", pool="test")
    assert waits is not None and waits.count == 6
    assert snapshot.gauge("blocking.queued", pool="test") == 0


def test_the_callers_contextvars_are_visible_in_the_worker_thread():
    adapter = make_adapter()
    marker = contextvars.ContextVar("marker", default="unset")

    async def scenario():
        marker.set("request-42")
        return await adapter.run(marker.get)

    try:
        assert asyncio.run(scenario()) == "request-42"
    finally:
        adapter.shutdown()


def test_exceptions_from_the_worker_reach_the_caller():
    adapter = make_adapter()

    def fail():
        raise ValueError("tool failed")

    async def scenario():
        await adapter.run(fail)

    try:
        with pytest.raises(ValueError, match="tool failed"):
            asyncio.run(scenario())
    finally:
        adapter.shutdown()


def test_a_cancelled_call_that_never_started_is_not_executed():
    adapter = make_adapter(workers=1)
    release = threading.Event()
    started = []

    def blocker():
        release.wait(2)

    def side_effect():
        started.append(True)

    async def scenario():
        running = asyncio.create_task(adapter.run(blocker))
        await asyncio.sleep(0.02)
        queued = asyncio.create_task(adapter.run(side_effect))
        await asyncio.sleep(0.02)
        assert adapter.queued == 1
        queued.cancel()
        await asyncio.gather(queued, return_exceptions=True)
        queued_after_cancel = adapter.queued
        release.set()
        await running
        await asyncio.sleep(0.05)
        return queued_after_cancel

    try:
        queued_after_cancel = asyncio.run(scenario())
    finally:
        release.set()
        adapter.shutdown()

    assert queued_after_cancel == 0
    assert started == []  # 有副作用的同步工具：等待方放弃之后不会再被执行


def test_a_call_already_running_when_cancelled_finishes_in_the_background():
    adapter = make_adapter()
    finished = threading.Event()

    def slow():
        time.sleep(0.1)
        finished.set()

    async def scenario():
        call = asyncio.create_task(adapter.run(slow))
        await asyncio.sleep(0.02)
        call.cancel()
        await asyncio.gather(call, return_exceptions=True)
        return finished.is_set()

    try:
        finished_at_cancel = asyncio.run(scenario())
        assert finished_at_cancel is False  # 取消不会假装线程已经停了
        assert finished.wait(2)  # 线程自己跑完，结果被丢弃
    finally:
        adapter.shutdown()


def test_shutdown_refuses_new_work_until_reopened():
    adapter = make_adapter()

    async def scenario():
        await adapter.run(lambda: None)
        adapter.shutdown()
        with pytest.raises(blocking.AdapterClosedError):
            await adapter.run(lambda: None)
        adapter.reopen()
        return await adapter.run(lambda: "back")

    try:
        assert asyncio.run(scenario()) == "back"
    finally:
        adapter.shutdown()


def test_pool_sizes_come_from_the_active_configuration(settings_override):
    settings_override(BLOCKING_TOOL_THREADS=3, BLOCKING_IO_THREADS=2)
    # 适配器按首次使用时的配置建线程池：先释放已有的线程池，让它按这份配置重建。
    blocking.shutdown_all()
    blocking.reopen_all()
    lock = threading.Lock()
    state = {"running": 0, "peak": 0}

    def work():
        with lock:
            state["running"] += 1
            state["peak"] = max(state["peak"], state["running"])
        time.sleep(0.05)
        with lock:
            state["running"] -= 1

    async def scenario():
        await asyncio.gather(*(blocking.tools().run(work) for _ in range(8)))

    try:
        asyncio.run(scenario())
    finally:
        blocking.shutdown_all()
        blocking.reopen_all()

    assert state["peak"] == 3


# -- 后台任务登记 -----------------------------------------------------------------------


def test_background_shutdown_cancels_pending_tasks_and_waits_for_them():
    tasks = background.BackgroundTasks()
    cancelled = []

    async def forever():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    async def scenario():
        for _ in range(3):
            tasks.spawn(forever(), name="pending")
        await asyncio.sleep(0.01)
        assert tasks.pending == 3
        count = await tasks.shutdown()
        return count, tasks.pending

    count, pending = asyncio.run(scenario())

    assert count == 3 and pending == 0
    assert len(cancelled) == 3


def test_background_shutdown_gives_short_tasks_a_grace_period_to_finish():
    tasks = background.BackgroundTasks()
    finished = []

    async def quick():
        await asyncio.sleep(0.05)
        finished.append(True)

    async def scenario():
        tasks.spawn(quick())
        return await tasks.shutdown(grace_seconds=1.0)

    assert asyncio.run(scenario()) == 0
    assert finished == [True]


def test_background_refuses_new_tasks_once_shutting_down_until_reopened():
    tasks = background.BackgroundTasks()
    ran = []

    async def work():
        ran.append(True)

    async def scenario():
        await tasks.shutdown()
        assert tasks.spawn(work()) is None
        tasks.reopen()
        task = tasks.spawn(work())
        assert task is not None
        await task

    asyncio.run(scenario())

    assert ran == [True]


def test_background_spawn_without_a_running_loop_drops_the_task_cleanly():
    tasks = background.BackgroundTasks()

    async def never_runs():
        raise AssertionError("must not run")

    assert tasks.spawn(never_runs()) is None


def test_a_failing_background_task_is_logged_and_forgotten(caplog):
    tasks = background.BackgroundTasks()

    async def broken():
        raise RuntimeError("summary exploded")

    async def scenario():
        task = tasks.spawn(broken(), name="summary-1")
        assert task is not None
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
        return tasks.pending

    with caplog.at_level(logging.ERROR):
        pending = asyncio.run(scenario())

    assert pending == 0
    assert "background task summary-1 failed" in caplog.text


# -- 同步 HTTP 会话登记 -----------------------------------------------------------------


def test_tracked_sessions_are_closed_together_and_a_failure_does_not_stop_the_rest():
    closed = []

    class Session:
        def __init__(self, name, fail=False):
            self.name = name
            self.fail = fail

        def close(self):
            if self.fail:
                raise RuntimeError("already broken")
            closed.append(self.name)

    http_sessions.close_tracked_sessions()
    sessions = [Session("a"), Session("b", fail=True), Session("c")]
    for session in sessions:
        assert http_sessions.track_session(session) is session

    assert http_sessions.tracked_count() == 3
    assert http_sessions.close_tracked_sessions() == 2
    assert sorted(closed) == ["a", "c"]
    assert http_sessions.tracked_count() == 0
