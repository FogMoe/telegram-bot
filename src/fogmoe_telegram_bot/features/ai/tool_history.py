"""工具循环产物的历史落库转换。

工具循环内已经配对好的消息序列（`LoopResult.turn_messages`）经 `turn_messages_to_record_entries`
脱敏成数据库记录：模型在循环里实际看到什么，历史里就存什么。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from fogmoe_telegram_bot.core.redaction import redact_text


def _telegram_command_name(arguments: object) -> str | None:
    if not isinstance(arguments, dict):
        return None
    command_text = str(arguments.get("command") or "").strip()
    if not command_text.startswith("/"):
        return None
    command_token = command_text.split(maxsplit=1)[0][1:]
    return command_token.split("@", maxsplit=1)[0].strip().lower() or None


def tool_logs_completed_clear(tool_logs: Sequence[dict[str, Any]]) -> bool:
    """返回本轮是否成功通过 Telegram 工具执行了 /clear。"""
    for tool_log in tool_logs:
        if not isinstance(tool_log, dict):
            continue
        if tool_log.get("type", "tool_result") != "tool_result":
            continue
        if tool_log.get("tool_name") != "execute_telegram_command":
            continue
        if _telegram_command_name(tool_log.get("arguments")) != "clear":
            continue
        result = tool_log.get("result")
        if isinstance(result, dict) and result.get("success") is True:
            return True
    return False


def _redacted_assistant_message(message: dict[str, Any]) -> dict[str, Any]:
    """脱敏要写入历史的 assistant 消息，只改文本与工具参数，保留 provider 专有字段。"""
    redacted = dict(message)
    if isinstance(redacted.get("content"), str):
        redacted["content"] = redact_text(redacted["content"])
    tool_calls = redacted.get("tool_calls")
    if isinstance(tool_calls, list):
        redacted_calls: list[Any] = []
        for tool_call in tool_calls:
            function = tool_call.get("function") if isinstance(tool_call, dict) else None
            if isinstance(function, dict) and isinstance(function.get("arguments"), str):
                tool_call = {
                    **tool_call,
                    "function": {
                        **function,
                        "arguments": redact_text(function["arguments"]),
                    },
                }
            redacted_calls.append(tool_call)
        redacted["tool_calls"] = redacted_calls
    return redacted


def turn_messages_to_record_entries(
    turn_messages: Sequence[dict[str, Any]],
) -> list[tuple[str, object]]:
    """直接从工具循环构建的消息序列脱敏生成数据库历史条目（纯函数，不修改输入）。"""
    record_entries: list[tuple[str, object]] = []
    for message in turn_messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "assistant":
            if message.get("tool_calls"):
                record_entries.append(("assistant", _redacted_assistant_message(message)))
            else:
                content = str(message.get("content") or "").strip()
                if content:
                    record_entries.append(("assistant", redact_text(content)))
        elif role == "tool":
            record_entries.append(
                (
                    "tool",
                    {
                        "role": "tool",
                        "tool_call_id": str(message.get("tool_call_id") or ""),
                        "name": message.get("name"),
                        "content": redact_text(str(message.get("content") or "")),
                    },
                )
            )
        elif role == "user":
            content = str(message.get("content") or "").strip()
            if content:
                record_entries.append(("user", content))
    return record_entries


def reply_record_entries(reply_text: str, runtime_error: str | None) -> list[tuple[str, object]]:
    """AI 最终回复写进历史的条目：脱敏后的回复；空回复与固定的错误提示不写。"""
    if not reply_text.strip() or runtime_error:
        return []
    return [("assistant", redact_text(reply_text))]
