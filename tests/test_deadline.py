"""整轮截止时间：协作式取消、宽限、提前到期与单次超时收紧。"""

import asyncio
import time

import pytest

from fogmoe_telegram_bot.core.deadline import (
    REASON_DEADLINE,
    REASON_SHUTDOWN,
    Deadline,
    DeadlineExceeded,
)


def test_remaining_time_counts_down_on_the_injected_clock():
    now = [100.0]
    deadline = Deadline(30, clock=lambda: now[0])

    assert deadline.remaining() == 30 and not deadline.expired
    now[0] = 129.5
    assert deadline.remaining() == pytest.approx(0.5)
    now[0] = 131.0
    assert deadline.remaining() == 0 and deadline.expired
    assert deadline.elapsed == pytest.approx(31.0)
    with pytest.raises(DeadlineExceeded):
        deadline.raise_if_expired()


def test_clip_tightens_a_per_call_timeout_but_keeps_a_floor():
    now = [0.0]
    deadline = Deadline(100, clock=lambda: now[0])

    assert deadline.clip(300) == 100
    assert deadline.clip(30) == 30
    assert deadline.clip(None) == 100
    now[0] = 99.9
    assert deadline.clip(300) == 1.0  # 剩余时间太短时至少留 1 秒，让请求还能发出


def test_guard_cancels_the_awaited_work_when_the_deadline_passes():
    cancelled = []

    async def hang():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    async def scenario():
        deadline = Deadline(0.05)
        started = time.monotonic()
        with pytest.raises(DeadlineExceeded) as exc_info:
            async with deadline.guard():
                await hang()
        return time.monotonic() - started, exc_info.value.reason

    elapsed, reason = asyncio.run(scenario())

    assert cancelled == [True]
    assert reason == REASON_DEADLINE
    assert elapsed < 1.0


def test_guard_lets_work_that_finishes_in_time_through():
    async def scenario():
        async with Deadline(5).guard():
            await asyncio.sleep(0.01)
            return "done"

    assert asyncio.run(scenario()) == "done"


def test_a_timeout_raised_inside_the_block_is_not_mistaken_for_the_deadline():
    async def scenario():
        async with Deadline(5).guard():
            raise TimeoutError("provider timed out on its own")

    with pytest.raises(TimeoutError, match="on its own"):
        asyncio.run(scenario())


def test_an_already_expired_deadline_refuses_to_start_the_block():
    async def scenario():
        deadline = Deadline(0.0)
        entered = []
        with pytest.raises(DeadlineExceeded):
            async with deadline.guard():
                entered.append(True)
        return entered

    assert asyncio.run(scenario()) == []


def test_grace_lets_the_notice_be_sent_after_the_deadline():
    async def scenario():
        deadline = Deadline(0.0)
        async with deadline.guard(extra=0.5):
            await asyncio.sleep(0.01)
            return "notice delivered"

    assert asyncio.run(scenario()) == "notice delivered"


def test_expire_wakes_a_waiting_guard_with_the_shutdown_reason():
    async def scenario():
        deadline = Deadline(60)

        async def stop_soon():
            await asyncio.sleep(0.05)
            deadline.expire(REASON_SHUTDOWN)

        stopper = asyncio.create_task(stop_soon())
        started = time.monotonic()
        with pytest.raises(DeadlineExceeded) as exc_info:
            async with deadline.guard():
                await asyncio.sleep(30)
        await stopper
        return time.monotonic() - started, exc_info.value.reason, deadline.expired

    elapsed, reason, expired = asyncio.run(scenario())

    assert reason == REASON_SHUTDOWN
    assert expired is True
    assert elapsed < 1.0


@pytest.mark.slow
def test_expire_keeps_the_grace_of_a_delivery_guard():
    async def scenario():
        deadline = Deadline(60)

        async def stop_soon():
            await asyncio.sleep(0.02)
            deadline.expire()

        stopper = asyncio.create_task(stop_soon())
        async with deadline.guard(extra=0.3):
            await asyncio.sleep(0.1)  # 提前到期之后仍在宽限内，不被取消
            result = "delivered"
        await stopper
        return result

    assert asyncio.run(scenario()) == "delivered"
