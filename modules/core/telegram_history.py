"""Telegram 可见事件到 AI 对话历史的统一记录层。"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Iterator

from telegram import Update
from telegram.ext import ExtBot

from . import group_chat_history, mysql_connection
from .prompt_utils import format_metadata_attrs, remove_xml_tags, xml_escape
from .telegram_utils import describe_message_for_context

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TelegramHistoryContext:
    user_id: int | None = None
    chat_id: int | None = None
    chat_type: str | None = None
    origin: str = "bot_automation"
    event: str = "automatic_reply"
    command: str | None = None
    cause: str | None = None
    redactions: tuple[str, ...] = ()


_HISTORY_CONTEXT: ContextVar[TelegramHistoryContext | None] = ContextVar(
    "telegram_history_context",
    default=None,
)
_CAPTURE_SUPPRESSED: ContextVar[bool] = ContextVar(
    "telegram_history_capture_suppressed",
    default=False,
)

_SENSITIVE_COMMAND_ARGUMENTS = {"charge", "webpassword"}
_SENSITIVE_COMMAND_OUTPUTS = {"create_code"}


def _format_timestamp(value: Any) -> str:
    if value and hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))


def _format_xml_attrs(attrs: list[tuple[str, str | None]]) -> str:
    return " ".join(
        f'{key}="{xml_escape(value)}"' for key, value in attrs if value
    )


def format_user_message(
    *,
    chat_type: str,
    chat_title: str | None,
    timestamp: str,
    user_name: str,
    message_text: str,
    message_id: str | int | None = None,
    edited: bool = False,
    edited_at: str | None = None,
    event: str | None = None,
    command: str | None = None,
    redacted: bool = False,
    forward_type: str | None = None,
    forward_origin_timestamp: str | None = None,
    forward_user: str | None = None,
    forward_name: str | None = None,
    forward_chat: str | None = None,
    forward_message_id: str | None = None,
    forward_author_signature: str | None = None,
    reply_user: str | None = None,
    reply_text: str | None = None,
    reply_type: str | None = None,
    reply_caption: str | None = None,
    reply_summary: str | None = None,
    reply_emoji: str | None = None,
    media_type: str | None = None,
    media_description: str | None = None,
    media_emoji: str | None = None,
) -> str:
    """格式化真实用户消息；只有这种记录包含 ``<message>``。"""
    attrs = [
        ("type", chat_type),
        ("timestamp", timestamp),
        ("user", f"@{user_name}"),
        ("message_id", str(message_id) if message_id is not None else None),
        ("event", event),
        ("command", command),
        ("redacted", "true" if redacted else None),
        ("edited", "true" if edited else None),
        ("edited_at", edited_at if edited else None),
    ]
    if chat_type in ("group", "supergroup") and chat_title:
        attrs.insert(1, ("title", chat_title))
    attr_text = format_metadata_attrs(attrs)
    lines = [f"<metadata {attr_text}>"]
    if forward_type:
        forward_attr_text = _format_xml_attrs(
            [
                ("type", forward_type),
                ("origin_timestamp", forward_origin_timestamp),
                ("user", forward_user),
                ("name", forward_name),
                ("chat", forward_chat),
                ("message_id", forward_message_id),
                ("author_signature", forward_author_signature),
            ]
        )
        lines.append(f"  <forward {forward_attr_text} />")
    if reply_type:
        reply_user_value = f"@{reply_user}" if reply_user else ""
        reply_attr_text = _format_xml_attrs(
            [
                ("user", reply_user_value),
                ("type", reply_type),
                ("emoji", reply_emoji),
            ]
        )
        lines.append(f"  <reply {reply_attr_text}>")
        if reply_text:
            lines.append(f"    <text>{xml_escape(remove_xml_tags(reply_text))}</text>")
        if reply_caption:
            lines.append(
                f"    <caption>{xml_escape(remove_xml_tags(reply_caption))}</caption>"
            )
        if reply_summary:
            lines.append(
                f"    <summary>{xml_escape(remove_xml_tags(reply_summary))}</summary>"
            )
        lines.append("  </reply>")
    elif reply_user or reply_text:
        reply_user_value = f"@{reply_user}" if reply_user else ""
        reply_attr = f' user="{xml_escape(reply_user_value)}"' if reply_user_value else ""
        lines.append(
            f"  <reply{reply_attr}>{xml_escape(remove_xml_tags(reply_text or ''))}</reply>"
        )
    if media_type:
        media_attrs = [("type", media_type)]
        if media_emoji:
            media_attrs.append(("emoji", media_emoji))
        media_attr_text = _format_xml_attrs(media_attrs)
        lines.append(f"  <media {media_attr_text}>")
        if media_description:
            lines.append(
                f"    <description>{xml_escape(media_description)}</description>"
            )
        lines.append("  </media>")
    lines.append("</metadata>")
    lines.append(f"<message>{xml_escape(remove_xml_tags(message_text))}</message>")
    return "\n".join(lines)


def format_bot_event(
    *,
    chat_type: str,
    chat_title: str | None,
    timestamp: str,
    origin: str,
    event: str,
    displayed_message: str,
    message_id: str | int | None = None,
    reply_to_message_id: str | int | None = None,
    command: str | None = None,
    cause: str | None = None,
    content_type: str | None = None,
    redacted: bool = False,
) -> str:
    """格式化 Telegram 运行时已经展示给用户的非 AI 事件。"""
    attrs = [
        ("type", "bot_event"),
        ("chat_type", chat_type),
        ("title", chat_title),
        ("timestamp", timestamp),
        ("origin", origin),
        ("event", event),
        ("command", command),
        ("cause", cause),
        ("content_type", content_type),
        ("redacted", "true" if redacted else None),
        ("message_id", str(message_id) if message_id is not None else None),
        (
            "reply_to_message_id",
            str(reply_to_message_id) if reply_to_message_id is not None else None,
        ),
    ]
    lines = [f"<metadata {format_metadata_attrs(attrs)}>"]
    if displayed_message:
        visible_text = xml_escape(remove_xml_tags(displayed_message))
        lines.append(f"  <displayed_message>{visible_text}</displayed_message>")
    lines.append("</metadata>")
    return "\n".join(lines)


def format_callback_event(update: Update) -> str | None:
    query = update.callback_query
    user = update.effective_user
    chat = update.effective_chat
    if not query or not user:
        return None

    label = None
    message = query.message
    markup = getattr(message, "reply_markup", None)
    for row in getattr(markup, "inline_keyboard", None) or ():
        for button in row:
            if getattr(button, "callback_data", None) == query.data:
                label = getattr(button, "text", None)
                break
        if label:
            break

    attrs = [
        ("type", "user_event"),
        ("chat_type", getattr(chat, "type", None)),
        ("title", getattr(chat, "title", None)),
        ("timestamp", _format_timestamp(datetime.now(timezone.utc))),
        ("user", f"@{user.username or 'EmptyUsername'}"),
        ("origin", "telegram"),
        ("event", "callback_query"),
        ("message_id", str(getattr(message, "message_id", "") or "")),
    ]
    callback_attrs = _format_xml_attrs(
        [
            ("data", str(query.data or "")),
            ("label", str(label) if label else None),
        ]
    )
    return "\n".join(
        [
            f"<metadata {format_metadata_attrs(attrs)}>",
            f"  <callback {callback_attrs} />",
            "</metadata>",
        ]
    )


def normalize_command_name(text: str | None) -> str | None:
    if not text or not text.startswith("/"):
        return None
    token = text.split(maxsplit=1)[0][1:]
    command = token.split("@", maxsplit=1)[0].strip().lower()
    return command or None


def _command_text_for_history(text: str, command: str) -> str:
    if command not in _SENSITIVE_COMMAND_ARGUMENTS:
        return text
    return f"/{command} [redacted]" if text.split(maxsplit=1)[1:] else f"/{command}"


def _command_redactions(text: str | None, command: str | None) -> tuple[str, ...]:
    if command not in _SENSITIVE_COMMAND_ARGUMENTS or not text:
        return ()
    arguments = text.split(maxsplit=1)
    if len(arguments) != 2 or not arguments[1]:
        return ()
    return (arguments[1],)


@contextmanager
def telegram_history_scope(**changes: Any) -> Iterator[None]:
    current = _HISTORY_CONTEXT.get() or TelegramHistoryContext()
    token = _HISTORY_CONTEXT.set(replace(current, **changes))
    try:
        yield
    finally:
        _HISTORY_CONTEXT.reset(token)


@contextmanager
def suppress_telegram_history() -> Iterator[None]:
    token = _CAPTURE_SUPPRESSED.set(True)
    try:
        yield
    finally:
        _CAPTURE_SUPPRESSED.reset(token)


async def _persist_event(user_id: int, content: str, bot: Any) -> None:
    try:
        snapshot_created, warning_level, archived_records = (
            await mysql_connection.async_insert_chat_record(user_id, "user", content)
        )
        if warning_level == "overflow":
            from features.ai import summary

            summary_text = await summary.generate_summary_immediately(user_id)
            if summary_text:
                await mysql_connection.async_update_latest_history_state_summary(
                    user_id,
                    summary_text,
                )
            else:
                summary.schedule_summary_generation(user_id)
        elif snapshot_created:
            from features.ai import summary

            summary.schedule_summary_generation(user_id)

        if archived_records:
            from .archive_utils import send_permanent_records_archive

            with suppress_telegram_history():
                await send_permanent_records_archive(
                    bot,
                    user_id,
                    archived_records,
                    logger=logger,
                )
    except Exception:
        logger.exception("记录 Telegram 元事件失败: user_id=%s", user_id)


async def record_command_update(update: Update, bot: Any) -> None:
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    command = normalize_command_name(getattr(message, "text", None))
    if not message or not user or not chat or not command:
        return

    command_text = _command_text_for_history(message.text or "", command)
    content = format_user_message(
        chat_type=chat.type or "private",
        chat_title=(chat.title or "").strip() or None,
        timestamp=_format_timestamp(message.date),
        user_name=user.username or "EmptyUsername",
        message_text=command_text,
        message_id=message.message_id,
        edited=update.edited_message is message,
        edited_at=_format_timestamp(getattr(message, "edit_date", None))
        if update.edited_message is message
        else None,
        event="command",
        command=command,
        redacted=(
            command in _SENSITIVE_COMMAND_ARGUMENTS
            and bool((message.text or "").split(maxsplit=1)[1:])
        ),
    )
    await _persist_event(user.id, content, bot)
    if chat.type in ("group", "supergroup"):
        try:
            await group_chat_history.log_group_message(message, chat.id)
        except Exception:
            logger.exception(
                "记录群聊命令失败: group_id=%s message_id=%s",
                chat.id,
                message.message_id,
            )


async def prepare_update_history(update: Update, context: Any) -> None:
    """在业务 handler 前设置出站事件来源，并记录非 AI 用户动作。"""
    user = update.effective_user
    chat = update.effective_chat
    message = update.effective_message
    command = normalize_command_name(getattr(message, "text", None))

    if command:
        origin = "command_handler"
        event = "command_reply"
    elif update.callback_query:
        origin = "bot_automation"
        event = "callback_reply"
    else:
        origin = "bot_automation"
        event = "automatic_reply"

    _HISTORY_CONTEXT.set(
        TelegramHistoryContext(
            user_id=getattr(user, "id", None),
            chat_id=getattr(chat, "id", None),
            chat_type=getattr(chat, "type", None),
            origin=origin,
            event=event,
            command=command,
            redactions=_command_redactions(getattr(message, "text", None), command),
        )
    )

    if command and command != "clear":
        await record_command_update(update, context.bot)
    elif update.callback_query and user:
        content = format_callback_event(update)
        if content:
            await _persist_event(user.id, content, context.bot)


async def _record_bot_message(bot: Any, message: Any) -> None:
    if _CAPTURE_SUPPRESSED.get() or not message:
        return

    chat = getattr(message, "chat", None)
    chat_type = getattr(chat, "type", None) or "private"
    context = _HISTORY_CONTEXT.get() or TelegramHistoryContext()
    target_chat_id = getattr(chat, "id", None)
    if context.chat_id is not None and context.chat_id != target_chat_id:
        context = TelegramHistoryContext(
            origin="bot_automation",
            event="automatic_reply",
        )
    description = describe_message_for_context(message)
    displayed_message = (
        description.get("text")
        or description.get("caption")
        or description.get("summary")
        or ""
    )
    output_redacted = context.command in _SENSITIVE_COMMAND_OUTPUTS
    if output_redacted:
        displayed_message = "[sensitive command output redacted]"
    else:
        for secret in context.redactions:
            if secret and secret in displayed_message:
                displayed_message = displayed_message.replace(secret, "[redacted]")
                output_redacted = True

    reply_to_message = getattr(message, "reply_to_message", None)
    content = format_bot_event(
        chat_type=chat_type,
        chat_title=(getattr(chat, "title", None) or "").strip() or None,
        timestamp=_format_timestamp(getattr(message, "date", None)),
        origin=context.origin,
        event=context.event,
        displayed_message=displayed_message,
        message_id=getattr(message, "message_id", None),
        reply_to_message_id=getattr(reply_to_message, "message_id", None),
        command=context.command,
        cause=context.cause,
        content_type=description.get("type"),
        redacted=output_redacted,
    )

    if chat_type == "private":
        conversation_id = getattr(chat, "id", None)
    elif context.chat_id == target_chat_id:
        conversation_id = context.user_id
    else:
        conversation_id = None

    if conversation_id is not None:
        await _persist_event(int(conversation_id), content, bot)
    if chat_type in ("group", "supergroup"):
        try:
            await group_chat_history.log_group_message(message, chat.id)
        except Exception:
            logger.exception(
                "记录群聊 Bot 消息失败: group_id=%s message_id=%s",
                getattr(chat, "id", None),
                getattr(message, "message_id", None),
            )


class HistoryTrackingExtBot(ExtBot):
    """记录成功发送到 Telegram 的可见 Bot 消息。"""

    async def _send_and_record(self, operation: Any) -> Any:
        result = await operation
        await _record_bot_message(self, result)
        return result

    async def send_message(self, *args: Any, **kwargs: Any) -> Any:
        return await self._send_and_record(super().send_message(*args, **kwargs))

    async def send_photo(self, *args: Any, **kwargs: Any) -> Any:
        return await self._send_and_record(super().send_photo(*args, **kwargs))

    async def send_document(self, *args: Any, **kwargs: Any) -> Any:
        return await self._send_and_record(super().send_document(*args, **kwargs))

    async def send_sticker(self, *args: Any, **kwargs: Any) -> Any:
        return await self._send_and_record(super().send_sticker(*args, **kwargs))

    async def send_audio(self, *args: Any, **kwargs: Any) -> Any:
        return await self._send_and_record(super().send_audio(*args, **kwargs))

    async def send_voice(self, *args: Any, **kwargs: Any) -> Any:
        return await self._send_and_record(super().send_voice(*args, **kwargs))

    async def edit_message_text(self, *args: Any, **kwargs: Any) -> Any:
        return await self._send_and_record(super().edit_message_text(*args, **kwargs))

    async def edit_message_caption(self, *args: Any, **kwargs: Any) -> Any:
        return await self._send_and_record(
            super().edit_message_caption(*args, **kwargs)
        )
