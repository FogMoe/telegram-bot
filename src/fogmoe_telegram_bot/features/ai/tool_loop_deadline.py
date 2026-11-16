"""工具循环的截止时间收尾：到期时怎样让这一轮的工具记录保持配对。

整轮截止时间在工具中途到期时，assistant 的每个 tool_call 仍要有一条结果，否则下一轮会因为不配对被
provider 拒绝，规则见 docs/runtime.md 的「整轮截止时间」。
"""

from __future__ import annotations

import contextlib
from typing import Any

from fogmoe_telegram_bot.core.deadline import Deadline

from .tool_loop_state import LoopState
from .types import ToolCallStatus

_INTERRUPTED_TOOL_RESULT = {
    "error": "interrupted",
    "outcome": "unknown",
    "message": (
        "The turn was stopped while this tool was running. It may or may not have "
        "completed; do not assume it failed and do not repeat it without checking."
    ),
}
_NOT_EXECUTED_TOOL_RESULT = {
    "error": "not_executed",
    "message": "The turn was stopped before this tool call could run.",
}


def deadline_guard(deadline: Deadline | None) -> Any:
    """没有截止时间时是空的异步上下文管理器。"""
    return contextlib.nullcontext() if deadline is None else deadline.guard()


def close_interrupted_round(
    state: LoopState,
    tool_calls: list[dict[str, Any]],
    first_open_index: int,
    reason: str,
    round_context_messages: list[dict[str, str]],
    *,
    running_record: int | None = None,
) -> None:
    """截止时间在这一轮工具中途到期：给每个还没有结果的工具调用补一条结果。

    `running_record` 是正在运行的那次调用的记录位置，标记为「结果未知」；从 `first_open_index`
    开始、还没来得及运行的调用标记为「未执行」。这样历史里 assistant 的 tool_calls 与 tool 结果仍然
    一一配对。这一轮已经完成的工具带回来的 Telegram 事件（例如代执行命令的回复）照常进入消息序列。
    """
    if running_record is not None:
        state.finish_tool_call(
            running_record,
            status=ToolCallStatus.INTERRUPTED,
            public_result={**_INTERRUPTED_TOOL_RESULT, "reason": reason},
        )
    for tool_call in tool_calls[first_open_index:]:
        function_payload = tool_call.get("function") or {}
        index = state.start_tool_call(
            str(function_payload.get("name") or "").strip(),
            tool_call["id"],
            {},
        )
        state.finish_tool_call(
            index,
            status=ToolCallStatus.NOT_EXECUTED,
            public_result=dict(_NOT_EXECUTED_TOOL_RESULT),
        )
    state.add_context_messages(round_context_messages)
