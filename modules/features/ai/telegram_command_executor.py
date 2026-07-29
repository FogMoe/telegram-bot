"""把 AI 工具请求安全地投递回 Telegram Application。"""

from __future__ import annotations

import asyncio
import itertools
import logging
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Any

from telegram import Update
from telegram.ext import CommandHandler

from core.telegram_history import (
    capture_telegram_history_events,
    delegated_telegram_command,
)

logger = logging.getLogger(__name__)

COMMAND_EXECUTION_TIMEOUT_SECONDS = 30.0
_OUTER_WAIT_MARGIN_SECONDS = 5.0

_RUNTIME_LOCK = Lock()
_APPLICATION: Any | None = None
_EVENT_LOOP: asyncio.AbstractEventLoop | None = None
_SYNTHETIC_UPDATE_IDS = itertools.count(1)


@dataclass(frozen=True)
class TelegramCommandOutcome:
    success: bool
    context_messages: tuple[str, ...] = ()
    error_code: str | None = None
    error_message: str | None = None


def configure_telegram_command_executor(
    application: Any,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """保存 Telegram Application 及其主事件循环。"""
    global _APPLICATION, _EVENT_LOOP
    with _RUNTIME_LOCK:
        _APPLICATION = application
        _EVENT_LOOP = loop


def registered_telegram_commands() -> set[str] | None:
    """返回当前 Application 注册的命令；运行时未就绪时返回 ``None``。"""
    with _RUNTIME_LOCK:
        application = _APPLICATION
    if application is None:
        return None

    commands: set[str] = set()
    for handlers in getattr(application, "handlers", {}).values():
        for handler in handlers:
            if isinstance(handler, CommandHandler):
                commands.update(str(command).lower() for command in handler.commands)
    return commands


def _build_synthetic_update(
    *,
    application: Any,
    command_text: str,
    request_context: dict[str, object],
) -> Update:
    user_id = int(request_context["user_id"])
    chat_id = int(request_context["chat_id"])
    chat_type = str(request_context["chat_type"])
    source_message_id = int(request_context["message_id"])
    command_token = command_text.split(maxsplit=1)[0]

    user_data: dict[str, object] = {
        "id": user_id,
        "is_bot": False,
        "first_name": str(
            request_context.get("first_name")
            or request_context.get("username")
            or "Telegram user"
        ),
    }
    username = request_context.get("username")
    if username:
        user_data["username"] = str(username)
    language_code = request_context.get("language_code")
    if language_code:
        user_data["language_code"] = str(language_code)

    chat_data: dict[str, object] = {
        "id": chat_id,
        "type": chat_type,
    }
    chat_title = request_context.get("chat_title")
    if chat_title:
        chat_data["title"] = str(chat_title)

    return Update.de_json(
        {
            "update_id": -next(_SYNTHETIC_UPDATE_IDS),
            "message": {
                # 复用真实请求的消息 ID，群聊中的 reply_text 才能引用存在的消息。
                "message_id": source_message_id,
                "date": int(datetime.now(timezone.utc).timestamp()),
                "chat": chat_data,
                "from": user_data,
                "text": command_text,
                "entities": [
                    {
                        "type": "bot_command",
                        "offset": 0,
                        "length": len(command_token),
                    }
                ],
            },
        },
        application.bot,
    )


def _execution_error(command: str, *, already_visible: bool = False) -> str:
    if already_visible:
        return (
            f"The Telegram handler for /{command} reported an execution error. "
            "A mechanical error message was already shown to the user. "
            "Do not claim success or repeat that message."
        )
    return (
        f"You could not execute /{command} on the user's behalf. "
        f"Tell the user to run /{command} themselves in Telegram. "
        "Do not retry this tool automatically or claim success."
    )


async def _execute_on_telegram_loop(
    *,
    application: Any,
    command: str,
    command_text: str,
    request_context: dict[str, object],
) -> TelegramCommandOutcome:
    user_id = int(request_context["user_id"])
    update = _build_synthetic_update(
        application=application,
        command_text=command_text,
        request_context=request_context,
    )

    with capture_telegram_history_events(user_id) as events:
        with delegated_telegram_command():
            try:
                await asyncio.wait_for(
                    application.process_update(update),
                    timeout=COMMAND_EXECUTION_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                return TelegramCommandOutcome(
                    success=False,
                    context_messages=tuple(events),
                    error_code="execution_unknown",
                    error_message=(
                        f"Execution of /{command} timed out and may be incomplete. "
                        "Do not retry automatically or claim success. Tell the user "
                        f"to run /{command} themselves if no mechanical reply appears."
                    ),
                )
            except Exception:
                logger.exception("AI 代执行 Telegram 命令失败: /%s", command)
                return TelegramCommandOutcome(
                    success=False,
                    context_messages=tuple(events),
                    error_code="execution_failed",
                    error_message=_execution_error(command),
                )

    context_messages = tuple(events)
    handler_error_visible = any(
        'type="bot_event"' in event and 'event="error_notice"' in event
        for event in context_messages
    )
    if handler_error_visible:
        return TelegramCommandOutcome(
            success=False,
            context_messages=context_messages,
            error_code="execution_failed",
            error_message=_execution_error(command, already_visible=True),
        )

    reply_visible = any('type="bot_event"' in event for event in context_messages)
    if not reply_visible:
        return TelegramCommandOutcome(
            success=False,
            context_messages=context_messages,
            error_code="execution_failed",
            error_message=_execution_error(command),
        )

    return TelegramCommandOutcome(
        success=True,
        context_messages=context_messages,
    )


def execute_telegram_command(
    *,
    command: str,
    command_text: str,
    request_context: dict[str, object],
) -> TelegramCommandOutcome:
    """从 AI executor 线程同步等待 Telegram 主循环完成命令。"""
    with _RUNTIME_LOCK:
        application = _APPLICATION
        loop = _EVENT_LOOP

    if application is None or loop is None or not loop.is_running():
        return TelegramCommandOutcome(
            success=False,
            error_code="execution_failed",
            error_message=_execution_error(command),
        )

    future = asyncio.run_coroutine_threadsafe(
        _execute_on_telegram_loop(
            application=application,
            command=command,
            command_text=command_text,
            request_context=request_context,
        ),
        loop,
    )
    try:
        return future.result(
            timeout=COMMAND_EXECUTION_TIMEOUT_SECONDS + _OUTER_WAIT_MARGIN_SECONDS
        )
    except FutureTimeoutError:
        future.cancel()
        logger.error("等待 Telegram 命令执行结果超时: /%s", command)
        return TelegramCommandOutcome(
            success=False,
            error_code="execution_unknown",
            error_message=(
                f"Execution status for /{command} is unknown. Do not retry "
                "automatically or claim success. Tell the user to run the command "
                "themselves if no mechanical reply appears."
            ),
        )
    except Exception:
        logger.exception("等待 Telegram 命令执行结果失败: /%s", command)
        return TelegramCommandOutcome(
            success=False,
            error_code="execution_failed",
            error_message=_execution_error(command),
        )


__all__ = [
    "TelegramCommandOutcome",
    "configure_telegram_command_executor",
    "execute_telegram_command",
    "registered_telegram_commands",
]
