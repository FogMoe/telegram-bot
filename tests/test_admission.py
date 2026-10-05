"""准入控制：全局槽位、有界排队、每用户待处理数与停止。"""

import asyncio
import time

import pytest

from fogmoe_telegram_bot.core import metrics
from fogmoe_telegram_bot.core.admission import (
    AdmissionController,
    AdmissionSettings,
    Overloaded,
    OverloadReason,
)
from fogmoe_telegram_bot.core.deadline import REASON_SHUTDOWN, Deadline, DeadlineExceeded


def make_controller(**overrides):
    settings = AdmissionSettings(**{"max_wait_seconds": 1.0, **overrides})
    return AdmissionController(lambda: settings)


@pytest.fixture(autouse=True)
def fresh_metrics():
    metrics.REGISTRY.reset()
    yield
    metrics.REGISTRY.reset()


def rejected(reason: str) -> int:
    return metrics.snapshot().counter("admission.rejected", reason=reason)


def test_requests_within_the_limit_run_immediately_with_no_wait():
    controller = make_controller(max_concurrent=2)

    async def scenario():
        async with controller.slot() as first, controller.slot() as second:
            assert controller.running == 2
            return first.waited, second.waited

    assert asyncio.run(scenario()) == (0.0, 0.0)
    assert controller.running == 0
    assert metrics.snapshot().counter("admission.admitted") == 2


@pytest.mark.slow
def test_requests_over_the_global_limit_queue_in_arrival_order():
    controller = make_controller(max_concurrent=1, max_queued=5)
    order = []

    async def worker(name, hold):
        async with controller.slot() as slot:
            order.append((name, slot.waited > 0))
            await asyncio.sleep(hold)

    async def scenario():
        first = asyncio.create_task(worker("first", 0.1))
        await asyncio.sleep(0.01)
        second = asyncio.create_task(worker("second", 0.01))
        await asyncio.sleep(0.01)
        third = asyncio.create_task(worker("third", 0.01))
        await asyncio.sleep(0.02)
        queued_while_waiting = controller.queued
        await asyncio.gather(first, second, third)
        return queued_while_waiting

    assert asyncio.run(scenario()) == 2
    assert [name for name, _ in order] == ["first", "second", "third"]
    assert order[0][1] is False and order[1][1] is True and order[2][1] is True
    snapshot = metrics.snapshot()
    waits = snapshot.histogram("admission.queue_seconds")
    assert waits is not None and waits.count == 3
    depth = snapshot.histogram("admission.queue_depth")  # 每个请求到达时看到的排队深度
    assert depth is not None and depth.count == 3 and depth.maximum == 1
    assert snapshot.gauge("admission.queued") == 0 and snapshot.gauge("admission.running") == 0


def test_a_request_that_waits_too_long_is_rejected_before_it_starts():
    controller = make_controller(max_concurrent=1, max_wait_seconds=0.05)

    async def scenario():
        async with controller.slot():
            started = time.monotonic()
            with pytest.raises(Overloaded) as exc_info:
                async with controller.slot():
                    pytest.fail("the second request must not start")
            return time.monotonic() - started, exc_info.value.reason

    elapsed, reason = asyncio.run(scenario())

    assert reason is OverloadReason.QUEUE_TIMEOUT
    assert 0.04 <= elapsed < 1.0
    assert rejected("queue_timeout") == 1
    assert controller.running == 0 and controller.queued == 0


def test_a_full_queue_rejects_immediately():
    controller = make_controller(max_concurrent=1, max_queued=1, max_wait_seconds=5)

    async def scenario():
        async with controller.slot():
            waiting = asyncio.create_task(controller._acquire(None))
            await asyncio.sleep(0.01)
            started = time.monotonic()
            with pytest.raises(Overloaded) as exc_info:
                async with controller.slot():
                    pytest.fail("must not start")
            elapsed = time.monotonic() - started
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
            return elapsed, exc_info.value.reason

    elapsed, reason = asyncio.run(scenario())

    assert reason is OverloadReason.QUEUE_FULL
    assert elapsed < 0.2
    assert rejected("queue_full") == 1
    assert controller.running == 0 and controller.queued == 0


def test_zero_wait_means_no_queueing_when_full():
    controller = make_controller(max_concurrent=1, max_wait_seconds=0)

    async def scenario():
        async with controller.slot():
            with pytest.raises(Overloaded) as exc_info:
                async with controller.slot():
                    pytest.fail("must not start")
            return exc_info.value.reason

    assert asyncio.run(scenario()) is OverloadReason.QUEUE_FULL


def test_the_wait_never_outlives_the_turn_deadline():
    controller = make_controller(max_concurrent=1, max_wait_seconds=30)

    async def scenario():
        async with controller.slot():
            started = time.monotonic()
            with pytest.raises(Overloaded) as exc_info:
                async with controller.slot(deadline=Deadline(0.05)):
                    pytest.fail("must not start")
            return time.monotonic() - started, exc_info.value.reason

    elapsed, reason = asyncio.run(scenario())

    assert reason is OverloadReason.DEADLINE
    assert elapsed < 1.0
    assert rejected("deadline") == 1


def test_an_already_expired_deadline_is_rejected_without_waiting():
    controller = make_controller()

    async def scenario():
        with pytest.raises(Overloaded) as exc_info:
            async with controller.slot(deadline=Deadline(0.0)):
                pytest.fail("must not start")
        return exc_info.value.reason

    assert asyncio.run(scenario()) is OverloadReason.DEADLINE


def test_a_cancelled_waiter_leaves_the_queue_and_never_leaks_a_slot():
    controller = make_controller(max_concurrent=1)

    async def scenario():
        async with controller.slot():
            waiter = asyncio.create_task(controller._acquire(None))
            await asyncio.sleep(0.01)
            assert controller.queued == 1
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
            assert controller.queued == 0
        # 持有者释放之后，新请求立刻拿到槽位：没有被取消的等待者占住。
        async with controller.slot() as slot:
            return slot.waited

    assert asyncio.run(scenario()) == 0.0
    assert controller.running == 0


def test_a_slot_granted_at_the_moment_of_cancellation_is_returned():
    controller = make_controller(max_concurrent=1)

    async def scenario():
        holder = await controller._acquire(None)
        waiter = asyncio.create_task(controller._acquire(None))
        await asyncio.sleep(0.01)
        controller._release(holder)  # 槽位已经分给等待者
        waiter.cancel()  # 等待者还没来得及消费就被取消
        await asyncio.gather(waiter, return_exceptions=True)
        return controller.running

    assert asyncio.run(scenario()) == 0


def test_per_user_pending_limit_rejects_the_overflow_and_counts_down_on_exit():
    controller = make_controller(max_pending_per_user=2)

    with controller.user_pending(7), controller.user_pending(7):
        assert controller.pending_for(7) == 2
        with pytest.raises(Overloaded) as exc_info:
            with controller.user_pending(7):
                pytest.fail("the third pending turn must be rejected")
        assert exc_info.value.reason is OverloadReason.USER_LIMIT
        # 别的用户不受影响
        with controller.user_pending(8):
            assert controller.pending_for(8) == 1

    assert controller.pending_for(7) == 0 and controller.pending_for(8) == 0
    assert rejected("user_limit") == 1


def test_requests_that_will_not_reach_the_model_are_not_counted_per_user():
    controller = make_controller(max_pending_per_user=1)

    with controller.user_pending(7):
        with controller.user_pending(7, counted=False):
            assert controller.pending_for(7) == 1


def test_per_user_pending_is_released_when_the_body_fails():
    controller = make_controller(max_pending_per_user=1)

    with pytest.raises(RuntimeError):
        with controller.user_pending(7):
            raise RuntimeError("boom")

    with controller.user_pending(7):
        assert controller.pending_for(7) == 1


@pytest.mark.slow
def test_the_per_user_limit_bounds_the_queue_behind_the_conversation_lock():
    """会话锁保证同一用户一次只跑一轮；名额限制的是排在锁后面的深度。"""
    controller = make_controller(max_concurrent=10, max_pending_per_user=2)
    lock = asyncio.Lock()
    ran = []

    async def turn(name, hold):
        with controller.user_pending(7):
            async with lock:
                ran.append(name)
                await asyncio.sleep(hold)

    async def scenario():
        first = asyncio.create_task(turn("first", 0.1))
        await asyncio.sleep(0.01)
        second = asyncio.create_task(turn("second", 0.01))
        await asyncio.sleep(0.01)
        # 一个在跑、一个在等锁：第三个在排队深度上被拒绝，不会再排到锁后面。
        with pytest.raises(Overloaded) as exc_info:
            await turn("third", 0)
        await asyncio.gather(first, second)
        return exc_info.value.reason

    assert asyncio.run(scenario()) is OverloadReason.USER_LIMIT
    assert ran == ["first", "second"]
    assert controller.pending_for(7) == 0


def test_close_rejects_queued_requests_immediately_and_future_ones():
    controller = make_controller(max_concurrent=1, max_wait_seconds=30)

    async def scenario():
        async with controller.slot():
            queued = asyncio.create_task(controller._acquire(None))
            await asyncio.sleep(0.01)
            started = time.monotonic()
            controller.close()
            with pytest.raises(Overloaded) as queued_exc:
                await queued
            rejected_in = time.monotonic() - started
            with pytest.raises(Overloaded) as new_exc:
                async with controller.slot():
                    pytest.fail("a closed controller admits nothing")
            with pytest.raises(Overloaded) as user_exc:
                with controller.user_pending(1):
                    pytest.fail("a closed controller admits nothing")
            return rejected_in, queued_exc.value.reason, new_exc.value.reason, user_exc.value.reason

    rejected_in, queued_reason, new_reason, user_reason = asyncio.run(scenario())

    assert rejected_in < 0.5
    assert queued_reason is new_reason is user_reason is OverloadReason.SHUTTING_DOWN
    assert controller.running == 0 and controller.queued == 0


@pytest.mark.slow
def test_shutdown_expires_in_flight_deadlines_after_the_grace_period():
    controller = make_controller(max_concurrent=2)

    async def scenario():
        deadline = Deadline(60)

        async def in_flight_turn():
            async with controller.slot(deadline=deadline):
                with pytest.raises(DeadlineExceeded) as exc_info:
                    async with deadline.guard():
                        await asyncio.sleep(30)
                return exc_info.value.reason

        turn = asyncio.create_task(in_flight_turn())
        await asyncio.sleep(0.02)
        started = time.monotonic()
        controller.begin_shutdown(grace_seconds=0.1)
        reason = await turn
        elapsed = time.monotonic() - started
        await controller.aclose()
        return reason, elapsed

    reason, elapsed = asyncio.run(scenario())

    assert reason == REASON_SHUTDOWN
    assert 0.08 <= elapsed < 2.0
    assert controller.closed


def test_a_turn_that_finishes_inside_the_grace_period_is_left_alone():
    controller = make_controller(max_concurrent=2)

    async def scenario():
        deadline = Deadline(60)
        async with controller.slot(deadline=deadline):
            controller.begin_shutdown(grace_seconds=5)
            await asyncio.sleep(0.02)
            expired = deadline.expired
        drained = await controller.drain(1.0)
        await controller.aclose()
        return expired, drained

    expired, drained = asyncio.run(scenario())

    assert expired is False
    assert drained is True


def test_settings_are_read_from_the_active_configuration(settings_override):
    settings_override(
        CHAT_MAX_CONCURRENT_TURNS=3,
        CHAT_MAX_QUEUED_TURNS=4,
        CHAT_MAX_PENDING_PER_USER=2,
        CHAT_QUEUE_MAX_WAIT_SECONDS=7,
        CHAT_TURN_DEADLINE_SECONDS=90,
        RUNTIME_SHUTDOWN_GRACE_SECONDS=11,
    )

    settings = AdmissionSettings.from_config()

    assert settings == AdmissionSettings(
        max_concurrent=3,
        max_queued=4,
        max_pending_per_user=2,
        max_wait_seconds=7.0,
        turn_deadline_seconds=90.0,
        shutdown_grace_seconds=11.0,
    )


def test_limits_changed_at_runtime_apply_to_the_next_request():
    current = {"settings": AdmissionSettings(max_concurrent=1, max_wait_seconds=0)}
    controller = AdmissionController(lambda: current["settings"])

    async def scenario():
        async with controller.slot():
            with pytest.raises(Overloaded):
                async with controller.slot():
                    pytest.fail("limit is 1")
            current["settings"] = AdmissionSettings(max_concurrent=2, max_wait_seconds=0)
            async with controller.slot():
                return controller.running

    assert asyncio.run(scenario()) == 2
