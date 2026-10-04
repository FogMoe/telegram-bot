"""有界的线程适配器：只给「必须同步执行」的代码用。

事件循环里不能做阻塞调用。能用原生 async 的一律用原生 async；确实只能同步的（requests、e2b、
binance 这类 SDK）通过这里的适配器放进有界线程池，并满足：

- **并发有上限**：线程池大小固定，超出的调用排队（排队深度与等待时间记录在指标里）。
- **取消语义**：等待的协程被取消时，还没开始的调用不会再执行；已经在线程里跑的无法中断，
  结果被丢弃，线程自己跑完。需要尽快退出的同步代码应当检查调用方传入的事件（见 `types.raise_if_aborted`）。
- **上下文**：调用时复制当前的 `contextvars`，线程里的工具能读到本次请求的 `tool_request_context`。
- **关停**：`shutdown()` 取消排队中的调用并释放线程池。

三个适配器：`tools()`（同步 AI 工具）、`io()`（事件循环里的回调需要的零星同步网络调用）与
`compute()`（每次模型调用前的 token 预算这类 CPU 密集的同步计算，避免占着事件循环）。
用途清单与理由见 docs/runtime.md 的「线程适配器清单」。
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from core import config, metrics

logger = logging.getLogger(__name__)

_QUEUED = "queued"
_RUNNING = "running"
_ABANDONED = "abandoned"


class AdapterClosedError(RuntimeError):
    """适配器已经关停（进程正在停止）。"""


class BoundedThreadAdapter:
    """固定大小的线程池 + 排队指标。线程池在首次使用时按当时的配置创建。"""

    def __init__(self, name: str, workers: Callable[[], int]) -> None:
        self.name = name
        self._workers = workers
        self._lock = threading.Lock()
        self._executor: ThreadPoolExecutor | None = None
        self._closed = False
        self._queued = 0
        self._running = 0

    @property
    def queued(self) -> int:
        return self._queued

    @property
    def running(self) -> int:
        return self._running

    @property
    def closed(self) -> bool:
        return self._closed

    def _get_executor(self) -> ThreadPoolExecutor:
        with self._lock:
            if self._closed:
                raise AdapterClosedError(f"blocking adapter {self.name!r} is shut down")
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=max(1, int(self._workers())),
                    thread_name_prefix=f"blocking-{self.name}",
                )
            return self._executor

    def _publish_depth(self) -> None:
        metrics.gauge("blocking.queued", pool=self.name).set(self._queued)
        metrics.gauge("blocking.running", pool=self.name).set(self._running)

    async def run[T](self, func: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
        """在线程池里运行 `func(*args, **kwargs)` 并等待结果。"""
        executor = self._get_executor()
        loop = asyncio.get_running_loop()
        context = contextvars.copy_context()
        submitted = time.monotonic()
        state = _QUEUED
        with self._lock:
            self._queued += 1
        self._publish_depth()

        def call() -> T | None:
            nonlocal state
            with self._lock:
                if state != _QUEUED:
                    return None  # 等待方已经放弃：不再执行
                state = _RUNNING
                self._queued -= 1
                self._running += 1
            self._publish_depth()
            metrics.histogram("blocking.wait_seconds", pool=self.name).observe(
                time.monotonic() - submitted
            )
            started = time.monotonic()
            try:
                return context.run(func, *args, **kwargs)
            finally:
                metrics.histogram("blocking.run_seconds", pool=self.name).observe(
                    time.monotonic() - started
                )
                with self._lock:
                    self._running -= 1
                self._publish_depth()

        try:
            result = await loop.run_in_executor(executor, call)
        except BaseException:
            with self._lock:
                if state == _QUEUED:
                    state = _ABANDONED
                    self._queued -= 1
            self._publish_depth()
            raise
        return result  # type: ignore[return-value]

    def shutdown(self) -> None:
        """取消排队中的调用，释放线程池；已经在跑的线程不等待。"""
        with self._lock:
            self._closed = True
            executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
        with self._lock:
            self._queued = 0
        self._publish_depth()

    def reopen(self) -> None:
        with self._lock:
            self._closed = False


_ADAPTERS: dict[str, BoundedThreadAdapter] = {}
_ADAPTERS_LOCK = threading.Lock()


def _adapter(name: str, workers: Callable[[], int]) -> BoundedThreadAdapter:
    with _ADAPTERS_LOCK:
        adapter = _ADAPTERS.get(name)
        if adapter is None:
            adapter = _ADAPTERS[name] = BoundedThreadAdapter(name, workers)
        return adapter


def tools() -> BoundedThreadAdapter:
    """同步 AI 工具用。大小 `BLOCKING_TOOL_THREADS`。"""
    return _adapter("tools", lambda: config.BLOCKING_TOOL_THREADS)


def io() -> BoundedThreadAdapter:
    """事件循环回调里零星的同步网络调用用。大小 `BLOCKING_IO_THREADS`。"""
    return _adapter("io", lambda: config.BLOCKING_IO_THREADS)


def compute() -> BoundedThreadAdapter:
    """CPU 密集的同步计算用，大小固定为 min(4, CPU 数)。"""
    return _adapter("compute", lambda: min(4, os.cpu_count() or 2))


def shutdown_all() -> None:
    with _ADAPTERS_LOCK:
        adapters = list(_ADAPTERS.values())
    for adapter in adapters:
        adapter.shutdown()


def reopen_all() -> None:
    with _ADAPTERS_LOCK:
        adapters = list(_ADAPTERS.values())
    for adapter in adapters:
        adapter.reopen()
