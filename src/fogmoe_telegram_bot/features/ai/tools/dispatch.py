"""工具的执行方式：async、同步（线程适配器）与内联。

每个工具在 `ToolSpec.execution` 里声明一种，`tool_loop_execution._call_tool` 按声明调用：

- **ASYNC**：async 函数，直接在事件循环里 `await`。访问数据库的工具、调用别的 async 能力的工具属于这一类。
- **THREAD**：同步函数，放进有界的线程适配器（`core.blocking.tools()`）执行，并发有上限。
  只能同步的 SDK（requests、e2b、urllib）属于这一类，清单与理由见 docs/runtime.md。
- **INLINE**：同步函数，直接在事件循环里同步调用。只能给纯内存、不做任何 I/O、微秒级完成的工具用
  （返回配置里的文本）。凡是可能阻塞的都不允许声明成内联。

声明与处理函数是否 `async def` 必须一致，由 `tests/test_tool_specs.py` 检查。
"""

from __future__ import annotations

from enum import StrEnum


class ToolExecution(StrEnum):
    ASYNC = "async"
    THREAD = "thread"
    INLINE = "inline"


__all__ = ["ToolExecution"]
