import json
import time
from typing import Any


def _tool_call_ids_from_message(message: dict[str, Any]) -> list[str]:
    call_ids: list[str] = []
    for tool_call in message.get("tool_calls") or []:
        if not isinstance(tool_call, dict):
            continue
        tool_call_id = tool_call.get("id")
        if tool_call_id:
            call_ids.append(str(tool_call_id))
    return call_ids


def _fallback_assistant_tool_call_message(
    tool_log: dict[str, Any],
    tool_call_id: str,
) -> dict[str, Any]:
    arguments = tool_log.get("arguments") or {}
    try:
        arguments_json = json.dumps(arguments, ensure_ascii=False)
    except TypeError:
        arguments_json = json.dumps({}, ensure_ascii=False)

    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": tool_call_id,
                "type": "function",
                "function": {
                    "name": tool_log.get("tool_name"),
                    "arguments": arguments_json,
                },
            }
        ],
    }


def _pop_pending_id(pending_tool_call_ids: list[str], tool_call_id: str) -> None:
    if pending_tool_call_ids and pending_tool_call_ids[0] == tool_call_id:
        pending_tool_call_ids.pop(0)
        return
    try:
        pending_tool_call_ids.remove(tool_call_id)
    except ValueError:
        return


def tool_logs_to_record_entries(
    tool_logs: list[dict[str, Any]],
) -> list[tuple[str, object]]:
    record_entries: list[tuple[str, object]] = []
    pending_tool_call_ids: list[str] = []

    for tool_log in tool_logs:
        entry_type = tool_log.get("type", "tool_result")
        if entry_type == "assistant_visible":
            visible_content = str(tool_log.get("content") or "").strip()
            if visible_content:
                record_entries.append(("assistant", visible_content))
            continue

        tool_call_id = tool_log.get("tool_call_id")
        if not tool_call_id:
            if entry_type == "tool_result" and pending_tool_call_ids:
                tool_call_id = pending_tool_call_ids.pop(0)
            else:
                tool_call_id = f"auto_{int(time.time() * 1000)}"
            tool_log["tool_call_id"] = tool_call_id
        tool_call_id = str(tool_call_id)

        if entry_type == "assistant_tool_call":
            assistant_message = tool_log.get("assistant_message")
            if isinstance(assistant_message, dict):
                call_ids = _tool_call_ids_from_message(assistant_message)
                if call_ids:
                    pending_tool_call_ids.extend(call_ids)
                else:
                    pending_tool_call_ids.append(tool_call_id)
                record_entries.append(("assistant", assistant_message))
                continue

            if tool_call_id in pending_tool_call_ids:
                continue

            pending_tool_call_ids.append(tool_call_id)
            record_entries.append(
                (
                    "assistant",
                    _fallback_assistant_tool_call_message(tool_log, tool_call_id),
                )
            )
            continue

        _pop_pending_id(pending_tool_call_ids, tool_call_id)
        tool_result = tool_log.get("result")
        try:
            tool_result_str = json.dumps(tool_result, ensure_ascii=False, default=str)
        except TypeError:
            tool_result_str = str(tool_result)

        record_entries.append(
            (
                "tool",
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "name": tool_log.get("tool_name"),
                    "content": tool_result_str,
                },
            )
        )

    return record_entries
