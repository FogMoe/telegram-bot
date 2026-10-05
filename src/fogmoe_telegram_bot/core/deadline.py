"""一轮工作的截止时间。

截止时间从「请求进入队列」开始计时，覆盖排队、provider 回退、工具与投递，规则见 docs/runtime.md。
`Deadline.guard()` 是协作式的取消点：到期时取消正在等待的那个 `await`（原生 async 的模型调用、
async 工具、Telegram 发送都是真正的任务取消），并抛出 `DeadlineExceeded`，由调用方决定怎样收尾。
线程里的同步工具无法被取消，调用方只是不再等它。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

REASON_DEADLINE = "deadline"
REASON_SHUTDOWN = "shutdown"


class DeadlineExceeded(Exception):
    """截止时间已到。`reason` 是 `REASON_DEADLINE`（超时）或 `REASON_SHUTDOWN`（进程正在停止）。"""

    def __init__(self, reason: str = REASON_DEADLINE, message: str | None = None) -> None:
        super().__init__(message or f"turn deadline reached ({reason})")
        self.reason = reason


class Deadline:
    """单调时钟上的一个截止点。`clock` 可注入，测试里用确定的时间。"""

    def __init__(
        self,
        seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self.seconds = float(seconds)
        self.started_at = clock()
        self.expires_at = self.started_at + self.seconds
        self.reason: str | None = None
        # 正在生效的 guard 及其宽限：提前到期时每个 guard 仍保留自己的宽限。
        self._guards: dict[asyncio.Timeout, float] = {}

    @classmethod
    def start(cls, seconds: float, *, clock: Callable[[], float] = time.monotonic) -> Deadline:
        return cls(seconds, clock=clock)

    def remaining(self) -> float:
        return max(self.expires_at - self._clock(), 0.0)

    @property
    def expired(self) -> bool:
        return self.remaining() <= 0.0

    @property
    def elapsed(self) -> float:
        return self._clock() - self.started_at

    @property
    def expiry_reason(self) -> str:
        return self.reason or REASON_DEADLINE

    def expire(self, reason: str = REASON_SHUTDOWN) -> None:
        """立刻让截止时间到期（进程停止时用）：正在等待的 guard 会在下一次事件循环迭代被取消。"""
        self.reason = reason
        self.expires_at = min(self.expires_at, self._clock())
        guards = list(self._guards.items())
        if not guards:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for guard, extra in guards:
            guard.reschedule(loop.time() + extra)

    def raise_if_expired(self) -> None:
        if self.expired:
            raise DeadlineExceeded(self.expiry_reason)

    def clip(self, timeout: float | None) -> float | None:
        """把单次调用的超时收紧到剩余时间之内；至少留 1 秒，让已经很短的剩余时间也能发出请求。"""
        remaining = max(self.remaining(), 1.0)
        return remaining if timeout is None else min(float(timeout), remaining)

    @asynccontextmanager
    async def guard(self, *, extra: float = 0.0) -> AsyncIterator[None]:
        """块内的 `await` 在截止时间到期时被取消，退出时抛 `DeadlineExceeded`。

        `extra` 是到期之后额外给的宽限（投递「超时提示」本身要用）。块内自己抛出的
        `TimeoutError` 原样通过：只有这个 guard 的计时器触发才转换。
        """
        if self.remaining() + extra <= 0:
            raise DeadlineExceeded(self.expiry_reason)
        timeout_cm = asyncio.timeout(self.remaining() + extra)
        try:
            async with timeout_cm:
                self._guards[timeout_cm] = extra
                try:
                    yield
                finally:
                    self._guards.pop(timeout_cm, None)
        except TimeoutError as exc:
            if timeout_cm.expired():
                raise DeadlineExceeded(self.expiry_reason) from exc
            raise
