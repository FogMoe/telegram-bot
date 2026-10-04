"""对话轮次的准入控制：全局并发上限、每用户待处理上限、有界的等待队列。

规则与取值依据见 docs/runtime.md 的「准入」。要点：

- **全局槽位**：同时运行的轮次不超过 `max_concurrent`；超出的在 FIFO 队列里等待，队列最长
  `max_queued`，每个请求最多等 `max_wait_seconds`（也不会超过自己的整轮截止时间）。
  队列满、等待超时、截止时间已到或进程正在停止，都抛 `Overloaded`，调用方在扣费之前告诉用户。
- **每用户待处理数**：同一个会话（用户）同时「正在处理 + 等待会话锁」的轮次不超过
  `max_pending_per_user`。会话锁本身保证同一用户一次只跑一轮；这个上限限制的是排在锁后面的深度。
- 槽位记录自己的 `Deadline`，进程停止时 `begin_shutdown` 可以让在途轮次的截止时间提前到期。
- 指标：`admission.running`、`admission.queued`、`admission.queue_depth`（每个请求到达时看到的排队深度）、
  `admission.queue_seconds`、`admission.admitted`、`admission.rejected{reason=...}`。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from core import config, metrics
from core.deadline import REASON_SHUTDOWN, Deadline

logger = logging.getLogger(__name__)

# 排队深度直方图的桶（个数，不是秒）。
_DEPTH_BUCKETS = (0, 1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024)


class OverloadReason(StrEnum):
    QUEUE_FULL = "queue_full"  # 等待队列已满，立即拒绝
    QUEUE_TIMEOUT = "queue_timeout"  # 等够了最长等待时间仍没有槽位
    DEADLINE = "deadline"  # 排队期间整轮截止时间已到
    USER_LIMIT = "user_limit"  # 这个用户待处理的轮次太多
    SHUTTING_DOWN = "shutting_down"  # 进程正在停止


class Overloaded(Exception):
    """准入被拒绝。抛出时一定还没有扣费，也没有开始这一轮。"""

    def __init__(self, reason: OverloadReason, message: str | None = None) -> None:
        super().__init__(message or f"admission rejected: {reason.value}")
        self.reason = reason


@dataclass(frozen=True, slots=True)
class AdmissionSettings:
    """准入读取的配置。`from_config` 在调用时取值，能看到 `config.install_settings`。"""

    max_concurrent: int = 32
    max_queued: int = 32
    max_pending_per_user: int = 3
    max_wait_seconds: float = 20.0
    turn_deadline_seconds: float = 360.0
    shutdown_grace_seconds: float = 20.0

    @classmethod
    def from_config(cls, source: Any = None) -> AdmissionSettings:
        settings = config if source is None else source
        return cls(
            max_concurrent=max(1, int(settings.CHAT_MAX_CONCURRENT_TURNS)),
            max_queued=max(0, int(settings.CHAT_MAX_QUEUED_TURNS)),
            max_pending_per_user=max(1, int(settings.CHAT_MAX_PENDING_PER_USER)),
            max_wait_seconds=max(0.0, float(settings.CHAT_QUEUE_MAX_WAIT_SECONDS)),
            turn_deadline_seconds=float(settings.CHAT_TURN_DEADLINE_SECONDS),
            shutdown_grace_seconds=max(0.0, float(settings.RUNTIME_SHUTDOWN_GRACE_SECONDS)),
        )


@dataclass(slots=True, eq=False)
class Slot:
    """一个运行中的全局槽位。`waited` 是为拿到它排队的秒数。"""

    waited: float
    deadline: Deadline | None = None


class AdmissionController:
    """进程内的准入控制器。限制每次从 `settings` 读取，配置变化立即生效。"""

    def __init__(
        self,
        settings: Callable[[], AdmissionSettings] = AdmissionSettings.from_config,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._running = 0
        self._waiters: deque[asyncio.Future[None]] = deque()
        self._slots: set[Slot] = set()
        self._pending_per_user: dict[int, int] = {}
        self._closed = False
        self._shutdown_task: asyncio.Task[None] | None = None

    # -- 状态 ---------------------------------------------------------------

    @property
    def running(self) -> int:
        return self._running

    @property
    def queued(self) -> int:
        return len(self._waiters)

    @property
    def closed(self) -> bool:
        return self._closed

    def pending_for(self, key: int) -> int:
        return self._pending_per_user.get(key, 0)

    def _publish_depth(self) -> None:
        metrics.gauge("admission.running").set(self._running)
        metrics.gauge("admission.queued").set(len(self._waiters))

    def reject(self, reason: OverloadReason) -> Overloaded:
        """记一次拒绝（指标）并返回对应的 `Overloaded`，由调用方 `raise`。"""
        metrics.counter("admission.rejected", reason=reason.value).inc()
        return Overloaded(reason)

    # -- 每用户待处理数 --------------------------------------------------------

    @contextmanager
    def user_pending(self, key: int, *, counted: bool = True) -> Iterator[None]:
        """登记这个用户多了一个待处理的轮次，超过上限抛 `Overloaded(USER_LIMIT)`。

        `counted=False` 的请求（例如不会唤起 AI 的群聊消息）不计数也不被拒绝。
        """
        if not counted:
            yield
            return
        if self._closed:
            raise self.reject(OverloadReason.SHUTTING_DOWN)
        limit = self._settings().max_pending_per_user
        current = self._pending_per_user.get(key, 0)
        if current >= limit:
            raise self.reject(OverloadReason.USER_LIMIT)
        self._pending_per_user[key] = current + 1
        try:
            yield
        finally:
            remaining = self._pending_per_user.get(key, 1) - 1
            if remaining > 0:
                self._pending_per_user[key] = remaining
            else:
                self._pending_per_user.pop(key, None)

    # -- 全局槽位 -------------------------------------------------------------

    @asynccontextmanager
    async def slot(self, *, deadline: Deadline | None = None) -> AsyncIterator[Slot]:
        """等待并占用一个全局槽位；拿不到就抛 `Overloaded`。块结束时释放。"""
        slot = await self._acquire(deadline)
        try:
            yield slot
        finally:
            self._release(slot)

    async def _acquire(self, deadline: Deadline | None) -> Slot:
        settings = self._settings()
        started = self._clock()
        if self._closed:
            raise self.reject(OverloadReason.SHUTTING_DOWN)
        if deadline is not None and deadline.expired:
            raise self.reject(OverloadReason.DEADLINE)
        metrics.REGISTRY.histogram("admission.queue_depth", buckets=_DEPTH_BUCKETS).observe(
            len(self._waiters)
        )

        if self._running < settings.max_concurrent and not self._waiters:
            return self._grant(0.0, deadline)

        if len(self._waiters) >= settings.max_queued or settings.max_wait_seconds <= 0:
            raise self.reject(OverloadReason.QUEUE_FULL)

        wait_budget = settings.max_wait_seconds
        deadline_limited = False
        if deadline is not None and deadline.remaining() <= wait_budget:
            wait_budget = deadline.remaining()
            deadline_limited = True

        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[None] = loop.create_future()
        self._waiters.append(waiter)
        self._publish_depth()
        try:
            async with asyncio.timeout(wait_budget):
                await waiter
        except TimeoutError:
            self._abandon(waiter)
            reason = OverloadReason.DEADLINE if deadline_limited else OverloadReason.QUEUE_TIMEOUT
            raise self.reject(reason) from None
        except BaseException:
            # 被取消（客户端的任务结束、进程停止）：已经轮到我们的话要把槽位还回去。
            self._abandon(waiter)
            raise

        if self._closed:
            self._release_running()
            raise self.reject(OverloadReason.SHUTTING_DOWN)
        return self._grant(max(self._clock() - started, 0.0), deadline, already_counted=True)

    def _grant(
        self,
        waited: float,
        deadline: Deadline | None,
        *,
        already_counted: bool = False,
    ) -> Slot:
        if not already_counted:
            self._running += 1
        slot = Slot(waited=waited, deadline=deadline)
        self._slots.add(slot)
        metrics.counter("admission.admitted").inc()
        metrics.histogram("admission.queue_seconds").observe(waited)
        self._publish_depth()
        return slot

    def _abandon(self, waiter: asyncio.Future[None]) -> None:
        """放弃一个等待者：还在队列里就移除；槽位已经分给它的话归还。"""
        try:
            self._waiters.remove(waiter)
        except ValueError:
            pass
        if waiter.done() and not waiter.cancelled() and waiter.exception() is None:
            self._release_running()
        else:
            waiter.cancel()
            self._publish_depth()

    def _release(self, slot: Slot) -> None:
        self._slots.discard(slot)
        self._release_running()

    def _release_running(self) -> None:
        self._running = max(self._running - 1, 0)
        self._wake_waiters()
        self._publish_depth()

    def _wake_waiters(self) -> None:
        limit = self._settings().max_concurrent
        while self._waiters and self._running < limit:
            waiter = self._waiters.popleft()
            if waiter.done():
                continue
            self._running += 1  # 先记账：等待者醒来之前槽位已经属于它
            waiter.set_result(None)

    # -- 停止 ---------------------------------------------------------------

    def close(self) -> None:
        """不再接受新的准入：排队的请求立刻被拒绝（还没扣费），在途的轮次不受影响。"""
        self._closed = True
        while self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.done():
                waiter.set_exception(self.reject(OverloadReason.SHUTTING_DOWN))
        self._publish_depth()

    def reopen(self) -> None:
        self._closed = False

    def begin_shutdown(self, grace_seconds: float | None = None) -> None:
        """停止接受新的准入，并在 `grace_seconds` 之后让所有在途轮次的截止时间到期。

        在途轮次的 `Deadline.guard` 因此在宽限结束时被取消，用户收到「正在重启」的提示。
        必须在事件循环内调用；重复调用只生效一次。
        """
        self.close()
        if self._shutdown_task is not None and not self._shutdown_task.done():
            return
        if grace_seconds is None:
            grace_seconds = self._settings().shutdown_grace_seconds
        self._shutdown_task = asyncio.get_running_loop().create_task(
            self._expire_after(grace_seconds)
        )

    async def _expire_after(self, grace_seconds: float) -> None:
        if grace_seconds > 0:
            await asyncio.sleep(grace_seconds)
        expired = 0
        for slot in list(self._slots):
            if slot.deadline is not None and not slot.deadline.expired:
                slot.deadline.expire(REASON_SHUTDOWN)
                expired += 1
        if expired:
            logger.warning(
                "shutdown grace of %ss elapsed; cancelling %s in-flight conversation turn(s)",
                grace_seconds,
                expired,
            )

    async def drain(self, timeout: float) -> bool:
        """等待在途轮次结束，最多 `timeout` 秒；返回是否已经全部结束。"""
        end = self._clock() + timeout
        while self._running > 0 and self._clock() < end:
            await asyncio.sleep(0.05)
        return self._running == 0

    async def aclose(self) -> None:
        """取消停止计时器（post_stop 里调用）。"""
        task, self._shutdown_task = self._shutdown_task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


_CONTROLLER: AdmissionController | None = None


def get_admission() -> AdmissionController:
    """进程唯一的准入控制器，首次使用时创建，限制随当时的配置读取。"""
    global _CONTROLLER
    if _CONTROLLER is None:
        _CONTROLLER = AdmissionController()
    return _CONTROLLER


def reset_admission() -> None:
    """丢弃当前控制器（下次使用重新创建）。给测试和重新启动用。"""
    global _CONTROLLER
    _CONTROLLER = None
