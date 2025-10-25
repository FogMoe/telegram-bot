"""Utilities for persisting and retrieving group chat context."""

from __future__ import annotations

import asyncio
import logging
import base64
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from mysql_connection import create_connection, db_executor

_bot_user_id: Optional[int] = None
_bot_display_name: str = "FogMoeBot"


def set_bot_identity(user_id: int, display_name: Optional[str] = None) -> None:
    """Register the bot's Telegram user id for downstream lookups."""
    global _bot_user_id, _bot_display_name
    _bot_user_id = user_id
    if display_name:
        _bot_display_name = display_name


async def log_group_message(message, group_id: int) -> None:
    """Persist a group chat message asynchronously."""
    if not group_id or not message:
        return

    user_id = getattr(message.from_user, "id", None)
    message_id = getattr(message, "message_id", None)
    if message_id is None:
        return

    message_type, content = _extract_message_payload(message)
    created_at = message.date or datetime.utcnow().replace(tzinfo=timezone.utc)

    record = (group_id, message_id, user_id, message_type, content, created_at)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(db_executor, lambda: _sync_log_group_message(record))


def _encode_non_text(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def _decode_non_text(value: str) -> str:
    try:
        return base64.b64decode(value.encode("ascii")).decode("utf-8")
    except Exception:
        return value


def _extract_message_payload(message) -> Tuple[str, str]:
    if getattr(message, "text", None):
        return "text", message.text

    if getattr(message, "caption", None):
        if message.photo:
            return "photo", _encode_non_text(message.caption)
        if message.video or message.animation:
            return "video", _encode_non_text(message.caption)
        if message.document:
            return "document", _encode_non_text(message.caption)
        return "other", _encode_non_text(message.caption)

    if getattr(message, "photo", None):
        return "photo", _encode_non_text("[photo]")
    if getattr(message, "sticker", None):
        emoji = getattr(message.sticker, "emoji", None)
        label = emoji or "[sticker]"
        return "sticker", _encode_non_text(label)
    if getattr(message, "voice", None):
        return "voice", _encode_non_text("[voice message]")
    if getattr(message, "video", None) or getattr(message, "animation", None):
        return "video", _encode_non_text("[video message]")
    if getattr(message, "document", None):
        file_name = getattr(message.document, "file_name", None)
        label = file_name or "[document]"
        return "document", _encode_non_text(label)

    return "other", _encode_non_text("[unsupported message]")


def _sync_log_group_message(record: Tuple[int, int, int, str, str, datetime]) -> None:
    group_id, message_id, user_id, message_type, content, created_at = record

    content = content or ""

    if created_at.tzinfo is not None:
        created_at = created_at.astimezone(timezone.utc).replace(tzinfo=None)

    connection = create_connection()
    cursor = connection.cursor()
    try:
        insert_sql = (
            "INSERT INTO chat_records_group (group_id, message_id, user_id, message_type, content, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)"
        )
        cursor.execute(insert_sql, (group_id, message_id, user_id, message_type, content, created_at))
        connection.commit()

        cleanup_sql = (
            "DELETE FROM chat_records_group "
            "WHERE group_id = %s AND id NOT IN ("
            "  SELECT id FROM ("
            "    SELECT id FROM chat_records_group "
            "    WHERE group_id = %s "
            "    ORDER BY created_at DESC, id DESC "
            "    LIMIT 100"
            "  ) AS recent"
            ")"
        )
        cursor.execute(cleanup_sql, (group_id, group_id))
        connection.commit()
    except Exception as exc:
        logging.error("Failed to log group message: %s", exc)
        connection.rollback()
    finally:
        cursor.close()
        connection.close()


def get_group_context(group_id: int, around_message_id: Optional[int] = None, window_size: int = 5) -> List[Dict[str, object]]:
    if not group_id:
        return []
    return _sync_get_group_context(group_id, around_message_id, window_size)


async def async_get_group_context(group_id: int, around_message_id: Optional[int] = None, window_size: int = 5) -> List[Dict[str, object]]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        db_executor,
        lambda: _sync_get_group_context(group_id, around_message_id, window_size)
    )


def _sync_get_group_context(group_id: int, around_message_id: Optional[int], window_size: int) -> List[Dict[str, object]]:
    connection = create_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        if around_message_id:
            cursor.execute(
                "SELECT cr.id, cr.message_id, cr.user_id, cr.message_type, cr.content, cr.created_at, u.name AS username "
                "FROM chat_records_group cr "
                "LEFT JOIN user u ON u.id = cr.user_id "
                "WHERE group_id = %s AND message_id <= %s "
                "ORDER BY created_at DESC, id DESC LIMIT %s",
                (group_id, around_message_id, window_size),
            )
            before = cursor.fetchall()

            cursor.execute(
                "SELECT cr.id, cr.message_id, cr.user_id, cr.message_type, cr.content, cr.created_at, u.name AS username "
                "FROM chat_records_group cr "
                "LEFT JOIN user u ON u.id = cr.user_id "
                "WHERE group_id = %s AND message_id > %s "
                "ORDER BY created_at ASC, id ASC LIMIT %s",
                (group_id, around_message_id, window_size),
            )
            after = cursor.fetchall()

            records = list(reversed(before)) + after
        else:
            cursor.execute(
                "SELECT cr.id, cr.message_id, cr.user_id, cr.message_type, cr.content, cr.created_at, u.name AS username "
                "FROM chat_records_group cr "
                "LEFT JOIN user u ON u.id = cr.user_id "
                "WHERE group_id = %s "
                "ORDER BY created_at DESC, id DESC LIMIT %s",
                (group_id, window_size),
            )
            records = list(reversed(cursor.fetchall()))

        return [
            {
                "message_id": row["message_id"],
                "user_id": row["user_id"],
                "message_type": row["message_type"],
                "username": (
                    _bot_display_name
                    if _bot_user_id is not None and row["user_id"] == _bot_user_id
                    else row.get("username")
                ),
                "content": (
                    row.get("content", "")
                    if row["message_type"] == "text"
                    else _decode_non_text(row.get("content", ""))
                ),
                "created_at": row["created_at"].isoformat(sep=" ") if row.get("created_at") else None,
            }
            for row in records
        ]
    except Exception as exc:
        logging.error("Failed to fetch group context: %s", exc)
        return []
    finally:
        cursor.close()
        connection.close()


__all__ = [
    "log_group_message",
    "get_group_context",
    "async_get_group_context",
    "set_bot_identity",
]
