"""工具的执行方式：async、同步（线程适配器）与内联。

工具注册表里的处理函数有三种，由 `tool_runner._call_tool` 区分：

- **async 函数**：直接在事件循环里 `await`。访问数据库的工具、调用别的 async 能力的工具属于这一类。
- **同步函数**（默认）：放进有界的线程适配器（`core.blocking.tools()`）执行，并发有上限。
  只能同步的 SDK（requests、e2b、urllib）属于这一类，清单与理由见 docs/runtime.md。
- **内联同步函数**：用 `@inline_tool` 标记，直接在事件循环里同步调用。只能给纯内存、不做任何
  I/O、微秒级完成的工具用（返回配置里的文本）。凡是可能阻塞的都不允许标记。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

_INLINE_ATTRIBUTE = "__runs_inline__"


def inline_tool[F: Callable[..., Any]](func: F) -> F:
    """标记一个同步工具直接在事件循环里调用（不进线程适配器）。"""
    setattr(func, _INLINE_ATTRIBUTE, True)
    return func


def is_inline_tool(handler: Callable[..., Any]) -> bool:
    return bool(getattr(handler, _INLINE_ATTRIBUTE, False))
