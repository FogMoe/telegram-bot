"""工具循环的截止时间收尾：到期时怎样让这一轮的工具记录保持配对。

整轮截止时间在工具中途到期时，assistant 的每个 tool_call 仍要有一条结果，否则下一轮会因为不配对被
provider 拒绝，规则见 docs/runtime.md 的「整轮截止时间」。一轮工具结果的日志条目（`tool_result_log`、
`log_round_context`）也放在这里，正常结束和中途到期写的是同一种条目。
"""

from __future__ import annotations

import contextlib
from typing import Any

from fogmoe_telegram_bot.core.deadline import Deadline

from .tool_loop_messages import tool_result_json
from .tools.specs import get_media_tool_spec
from .types import MEDIA_DELIVERY_KEY, MediaDeliveryState, ToolLog

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
    tool_logs: list[ToolLog],
    tool_calls: list[dict[str, Any]],
    interrupted_index: int,
    reason: str,
    round_context_messages: list[dict[str, str]],
    *,
    running: bool = True,
    filtered_messages: list[dict[str, Any]] | None = None,
) -> None:
    """截止时间在这一轮工具中途到期：给每个还没有结果的工具调用补一条结果。

    正在运行的那个（`running`）标记为「结果未知」，后面没来得及运行的标记为「未执行」，
    这样历史里 assistant 的 tool_calls 与 tool 结果仍然一一配对。`running=False` 表示
    `interrupted_index` 那个调用还没开始（前一个工具已经有结果，是在投递它的媒体时到期的）。
    这一轮已经完成的工具带回来的 Telegram 事件（例如代执行命令的回复）照常记进消息序列与日志。
    """
    for index, tool_call in enumerate(tool_calls):
        if index < interrupted_index:
            continue
        function_payload = tool_call.get("function") or {}
        function_name = str(function_payload.get("name") or "").strip()
        tool_call_id = tool_call.get("id")
        if index == interrupted_index and running:
            result = {**_INTERRUPTED_TOOL_RESULT, "reason": reason}
        else:
            result = dict(_NOT_EXECUTED_TOOL_RESULT)
        if filtered_messages is not None:
            filtered_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "name": function_name,
                    "content": tool_result_json(result),
                }
            )
        tool_logs.append(
            {
                "type": "tool_result",
                "tool_name": function_name,
                "arguments": {},
                "result": result,
                "tool_call_id": tool_call_id,
            }
        )
    if filtered_messages is not None:
        filtered_messages.extend(round_context_messages)
    log_round_context(tool_logs, round_context_messages)


def log_round_context(
    tool_logs: list[ToolLog],
    round_context_messages: list[dict[str, str]],
) -> None:
    for context_message in round_context_messages:
        tool_logs.append(
            {
                "type": "telegram_event",
                "role": "user",
                "content": context_message["content"],
            }
        )


def tool_result_log(
    function_name: str,
    function_args: dict[str, Any],
    tool_call_id: Any,
    tool_result: dict[str, Any],
    internal_tool_result: dict[str, Any],
    *,
    sent_media_messages: list[Any],
    media_delivery_unknown: bool = False,
) -> ToolLog:
    tool_log_entry: ToolLog = {
        "type": "tool_result",
        "tool_name": function_name,
        "arguments": function_args,
        "result": tool_result,
        "tool_call_id": tool_call_id,
    }
    if get_media_tool_spec(function_name) is not None:
        tool_log_entry["internal_result"] = internal_tool_result
        if media_delivery_unknown:
            tool_log_entry[MEDIA_DELIVERY_KEY] = MediaDeliveryState.UNKNOWN.value
        elif sent_media_messages:
            tool_log_entry[MEDIA_DELIVERY_KEY] = MediaDeliveryState.SENT.value
            tool_log_entry["media_sent"] = True
            tool_log_entry["sent_message_count"] = len(sent_media_messages)
        elif isinstance(internal_tool_result, dict) and internal_tool_result.get("status") == "generated":
            tool_log_entry[MEDIA_DELIVERY_KEY] = MediaDeliveryState.PENDING.value
    return tool_log_entry
