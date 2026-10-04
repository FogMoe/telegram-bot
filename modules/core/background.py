"""后台任务登记：发出去不等的任务必须能在关停时被取消和等待。

`spawn(coro)` 代替裸的 `asyncio.create_task`：任务被登记，异常被记录（经脱敏），`shutdown()` 按
「停止接收 -> 给一小段宽限 -> 取消剩余 -> 等它们真正结束」的顺序收尾。关停顺序见 docs/runtime.md。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

from core.redaction import log_exception

logger = logging.getLogger(__name__)


class BackgroundTasks:
    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closed = False

    @property
    def pending(self) -> int:
        return len(self._tasks)

    @property
    def closed(self) -> bool:
        return self._closed

    def spawn[T](
        self,
        coro: Coroutine[Any, Any, T],
        *,
        name: str | None = None,
    ) -> asyncio.Task[T] | None:
        """登记并启动一个后台任务；已经停止接收、或不在事件循环里时丢弃并返回 None。"""
        if self._closed:
            coro.close()
            logger.debug("background task %s dropped: shutting down", name)
            return None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            coro.close()
            logger.warning("background task %s dropped: no running event loop", name)
            return None
        task = loop.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._finished)
        return task

    def _finished(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            log_exception(logger, f"background task {task.get_name()} failed", error)

    async def shutdown(self, *, grace_seconds: float = 0.0) -> int:
        """停止接收新任务，等待至多 `grace_seconds`，再取消剩下的并等它们结束。返回被取消的个数。"""
        self._closed = True
        tasks = [task for task in self._tasks if not task.done()]
        if not tasks:
            return 0
        if grace_seconds > 0:
            _, tasks_left = await asyncio.wait(tasks, timeout=grace_seconds)
            tasks = list(tasks_left)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return len(tasks)

    def reopen(self) -> None:
        self._closed = False


# 进程唯一的登记处。
BACKGROUND = BackgroundTasks()


def spawn[T](coro: Coroutine[Any, Any, T], *, name: str | None = None) -> asyncio.Task[T] | None:
    return BACKGROUND.spawn(coro, name=name)
