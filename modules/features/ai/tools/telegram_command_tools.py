"""供主 AI 代用户执行 Telegram 命令的工具。"""

from __future__ import annotations

import re
from typing import Any

from features.ai.telegram_command_executor import (
    execute_telegram_command,
    registered_telegram_commands,
)
from features.ai.types import TOOL_CONTEXT_MESSAGES_KEY

from .context import get_tool_request_context

_COMMAND_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_REQUIRED_CONTEXT_FIELDS = ("user_id", "chat_id", "chat_type", "message_id")
_REQUEST_CACHE_KEY = "_executed_telegram_commands"


def _error(code: str, message: str) -> dict[str, Any]:
    return {
        "success": False,
        "error": {
            "code": code,
            "message": message,
        },
    }


def _without_context_messages(result: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in result.items()
        if key != TOOL_CONTEXT_MESSAGES_KEY
    }


def execute_telegram_command_tool(
    command: str,
    **kwargs: object,
) -> dict[str, Any]:
    command_input = str(command or "").strip()
    if "\n" in command_input or "\r" in command_input:
        return _error(
            "invalid_command",
            "Pass one complete Telegram command as a single line.",
        )

    command_parts = command_input.split(maxsplit=1)
    command_token = command_parts[0] if command_parts else ""
    command_value = command_token[1:].lower() if command_token.startswith("/") else ""
    if not _COMMAND_NAME_PATTERN.fullmatch(command_value):
        return _error(
            "invalid_command",
            (
                "Invalid Telegram command. Pass the complete command including "
                "the leading '/'."
            ),
        )

    registered_commands = registered_telegram_commands()
    if registered_commands is None:
        return _error(
            "execution_failed",
            (
                f"You cannot execute /{command_value} on the user's behalf because "
                "the Telegram command runtime is unavailable. Tell the user to run "
                f"/{command_value} themselves in Telegram. Do not retry this tool."
            ),
        )

    if command_value not in registered_commands:
        return _error(
            "unknown_command",
            (
                f"/{command_value} is not a registered Telegram command. "
                "Use get_help_text to find the correct command, then retry with "
                "the corrected complete command. If no matching command exists, "
                "tell the user it is unavailable."
            ),
        )

    request_context = get_tool_request_context()
    missing_fields = [
        field
        for field in _REQUIRED_CONTEXT_FIELDS
        if request_context.get(field) is None
    ]
    if missing_fields:
        return _error(
            "execution_failed",
            (
                f"You cannot execute /{command_value} on the user's behalf in this "
                "runtime context. Tell the user to run the command themselves in "
                "Telegram. Do not retry this tool."
            ),
        )

    cache = request_context.setdefault(_REQUEST_CACHE_KEY, {})
    command_text = command_input
    cache_key = command_text
    if isinstance(cache, dict) and cache_key in cache:
        cached_result = cache[cache_key]
        if isinstance(cached_result, dict):
            return dict(cached_result)

    outcome = execute_telegram_command(
        command=command_value,
        command_text=command_text,
        request_context=request_context,
    )
    if outcome.success:
        result: dict[str, Any] = {"success": True}
    else:
        result = _error(
            outcome.error_code or "execution_failed",
            outcome.error_message
            or (
                f"You could not execute /{command_value} on the user's behalf. "
                f"Tell the user to run /{command_value} themselves in Telegram."
            ),
        )

    if outcome.context_messages:
        result[TOOL_CONTEXT_MESSAGES_KEY] = list(outcome.context_messages)

    if isinstance(cache, dict):
        cache[cache_key] = _without_context_messages(result)
    return result


__all__ = ["execute_telegram_command_tool"]
