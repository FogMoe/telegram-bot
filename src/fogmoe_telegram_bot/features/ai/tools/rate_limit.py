"""AI 工具共用的进程内限流、冷却与并发闸门。

所有状态只驻留在当前进程内存里（单进程部署，见 docs/architecture.md），并在每次检查时顺手清理过期键，
避免长期运行后按 `user_id` 无限增长。限流阈值由调用方在每次判定时传入。
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from typing import NamedTuple, Protocol


class Resettable(Protocol):
    def reset(self) -> None: ...


_REGISTERED: list[Resettable] = []
_REGISTRY_LOCK = threading.Lock()


def _register[T: Resettable](limiter: T) -> T:
    with _REGISTRY_LOCK:
        _REGISTERED.append(limiter)
    return limiter


def reset_all() -> None:
    """清空所有已注册工具限流器的内存状态（供测试隔离使用）。"""
    with _REGISTRY_LOCK:
        limiters = list(_REGISTERED)
    for limiter in limiters:
        limiter.reset()


class RateLimitReservation(NamedTuple):
    """滑动窗口占座结果。`token` 用于上游调用失败时回滚本次占座。"""

    allowed: bool
    token: float | None
    retry_after_seconds: int | None


class SlidingWindowRateLimiter:
    """按用户（或任意键）计数的线程安全滑动时间窗限流器。"""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self.buckets: dict[str, list[float]] = {}
        _register(self)

    def _prune_unlocked(self, cutoff: float) -> None:
        for existing_key, timestamps in list(self.buckets.items()):
            active = [ts for ts in timestamps if ts > cutoff]
            if active:
                self.buckets[existing_key] = active
            else:
                self.buckets.pop(existing_key, None)

    def reserve(
        self,
        key: object,
        *,
        window_seconds: float,
        max_calls: int,
        now: float | None = None,
    ) -> RateLimitReservation:
        current_time = self._clock() if now is None else now
        cutoff = current_time - window_seconds
        bucket_key = str(key)

        with self._lock:
            self._prune_unlocked(cutoff)
            timestamps = list(self.buckets.get(bucket_key, []))
            if len(timestamps) >= max_calls:
                self.buckets[bucket_key] = timestamps
                retry_after = math.ceil(
                    max(1.0, window_seconds - (current_time - timestamps[0]))
                )
                return RateLimitReservation(False, None, retry_after)

            timestamps.append(current_time)
            self.buckets[bucket_key] = timestamps
            return RateLimitReservation(True, current_time, None)

    def release(self, key: object, token: float | None) -> None:
        if token is None:
            return
        bucket_key = str(key)
        with self._lock:
            timestamps = self.buckets.get(bucket_key)
            if not timestamps:
                return
            for index, ts in enumerate(timestamps):
                if ts == token:
                    del timestamps[index]
                    break
            if timestamps:
                self.buckets[bucket_key] = timestamps
            else:
                self.buckets.pop(bucket_key, None)

    def reset(self) -> None:
        with self._lock:
            self.buckets.clear()


class NonBlockingConcurrencyLimiter:
    """不排队的全局并发上限：满了立即返回 False，由工具向模型返回 busy。"""

    def __init__(self, limit_getter: Callable[[], int]) -> None:
        self._limit_getter = limit_getter
        self._lock = threading.Lock()
        self._inflight = 0
        _register(self)

    @property
    def inflight(self) -> int:
        with self._lock:
            return self._inflight

    def acquire(self, blocking: bool = False) -> bool:
        del blocking
        limit = max(0, int(self._limit_getter()))
        with self._lock:
            if self._inflight >= limit:
                return False
            self._inflight += 1
            return True

    def release(self) -> None:
        with self._lock:
            self._inflight = max(self._inflight - 1, 0)

    def reset(self) -> None:
        with self._lock:
            self._inflight = 0


class SessionReservation(NamedTuple):
    allowed: bool
    blocked_reason: str | None
    retry_after_seconds: int | None


class UserSessionGate:
    """有状态会话型工具（如 E2B 沙箱）的每用户互斥、全局会话上限与创建冷却。"""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self.active_users: dict[str, float] = {}
        self.cooldowns: dict[str, float] = {}
        _register(self)

    def _purge_unlocked(self, now: float) -> None:
        for user_id, expires_at in list(self.active_users.items()):
            if expires_at <= now:
                self.active_users.pop(user_id, None)
        for user_id, cooldown_until in list(self.cooldowns.items()):
            if cooldown_until <= now:
                self.cooldowns.pop(user_id, None)

    def reserve(
        self,
        user_key: str,
        *,
        active_ttl_seconds: float,
        cooldown_seconds: float,
        max_concurrent_sessions: int,
        now: float | None = None,
    ) -> SessionReservation:
        current_time = self._clock() if now is None else now
        with self._lock:
            self._purge_unlocked(current_time)
            if user_key in self.active_users:
                return SessionReservation(False, "user_sandbox_busy", None)
            if len(self.active_users) >= max_concurrent_sessions:
                return SessionReservation(False, "global_sandbox_busy", None)

            cooldown_until = self.cooldowns.get(user_key)
            if cooldown_until is not None:
                retry_after = max(1, int(cooldown_until - current_time))
                return SessionReservation(False, "user_rate_limit", retry_after)

            self.active_users[user_key] = current_time + active_ttl_seconds
            self.cooldowns[user_key] = current_time + cooldown_seconds
            return SessionReservation(True, None, None)

    def release_active(self, user_key: str) -> None:
        """会话结束或没建起来：释放活跃锁，创建冷却照常保留。"""
        with self._lock:
            self.active_users.pop(str(user_key), None)

    def reset(self) -> None:
        with self._lock:
            self.active_users.clear()
            self.cooldowns.clear()


__all__ = [
    "NonBlockingConcurrencyLimiter",
    "RateLimitReservation",
    "SessionReservation",
    "SlidingWindowRateLimiter",
    "UserSessionGate",
    "reset_all",
]
