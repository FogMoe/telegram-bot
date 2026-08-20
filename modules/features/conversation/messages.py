"""把 Telegram 消息整理成 AI 能读的形态。

包含取实际消息体、编辑去重指纹、媒体类型判定，以及回复/转发的元数据格式化。
"""

import logging
from collections import OrderedDict

from telegram import Update

from core.telegram_utils import (
    describe_forward_for_context,
    describe_message_for_context,
)

logger = logging.getLogger(__name__)


_MESSAGE_CONTENT_FINGERPRINT_LIMIT = 4096


_MESSAGE_CONTENT_FINGERPRINTS: OrderedDict[
    tuple[int, int],
    tuple[str, str, tuple[str, ...], str, str],
] = OrderedDict()


def get_effective_message(update: Update):
    """获取有效的消息对象，无论是普通消息还是编辑后的消息"""
    return update.message or update.edited_message


def _media_file_identifier(value) -> str:
    if value is None:
        return ""
    return str(
        getattr(value, "file_unique_id", None)
        or getattr(value, "file_id", None)
        or ""
    )


def _message_content_fingerprint(message) -> tuple[str, str, tuple[str, ...], str, str]:
    photo_ids = tuple(
        _media_file_identifier(photo)
        for photo in (getattr(message, "photo", None) or ())
    )
    sticker = getattr(message, "sticker", None)
    return (
        str(getattr(message, "text", None) or ""),
        str(getattr(message, "caption", None) or ""),
        photo_ids,
        _media_file_identifier(sticker),
        str(getattr(sticker, "emoji", None) or ""),
    )


def _record_message_content_and_check_unchanged_edit(update: Update) -> bool:
    message = get_effective_message(update)
    chat = update.effective_chat
    message_id = getattr(message, "message_id", None) if message else None
    if not message or not chat or message_id is None:
        return False

    key = (chat.id, message_id)
    fingerprint = _message_content_fingerprint(message)
    previous_fingerprint = _MESSAGE_CONTENT_FINGERPRINTS.get(key)
    _MESSAGE_CONTENT_FINGERPRINTS[key] = fingerprint
    _MESSAGE_CONTENT_FINGERPRINTS.move_to_end(key)
    while len(_MESSAGE_CONTENT_FINGERPRINTS) > _MESSAGE_CONTENT_FINGERPRINT_LIMIT:
        _MESSAGE_CONTENT_FINGERPRINTS.popitem(last=False)

    return (
        update.edited_message is message
        and previous_fingerprint is not None
        and previous_fingerprint == fingerprint
    )


def _format_message_timestamp(value) -> str | None:
    if not value:
        return None
    if hasattr(value, "strftime"):
        return value.strftime('%Y-%m-%d %H:%M:%S')
    return str(value)


def _media_mime_type(media_type: str, effective_message) -> str | None:
    if media_type == "photo":
        return "image/jpeg"
    if media_type == "sticker":
        sticker = effective_message.sticker
        if getattr(sticker, "is_animated", False) or getattr(sticker, "is_video", False):
            return None
        return "image/webp"
    return None


def _build_reply_format_kwargs(reply_message) -> dict[str, str | None]:
    description = describe_message_for_context(reply_message)
    quoted_user = (
        getattr(getattr(reply_message, "from_user", None), "username", None)
        or "EmptyUsername"
    )

    if description.get("type") == "text":
        return {
            "reply_user": quoted_user,
            "reply_text": description.get("text") or "",
        }

    return {
        "reply_user": quoted_user,
        "reply_type": description.get("type") or "other",
        "reply_caption": description.get("caption"),
        "reply_summary": description.get("summary"),
        "reply_emoji": description.get("emoji"),
    }


def _build_forward_format_kwargs(message) -> dict[str, str | None]:
    description = describe_forward_for_context(message)
    if not description:
        return {}
    return {
        "forward_type": description.get("type"),
        "forward_origin_timestamp": description.get("origin_timestamp"),
        "forward_user": description.get("user"),
        "forward_name": description.get("name"),
        "forward_chat": description.get("chat"),
        "forward_message_id": description.get("message_id"),
        "forward_author_signature": description.get("author_signature"),
    }


def _build_multimodal_user_message(
    formatted_message: str,
    *,
    base64_str: str,
    mime_type: str | None,
) -> dict | None:
    if not mime_type:
        return None
    return {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": formatted_message,
            },
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime_type};base64,{base64_str}",
                },
            },
        ],
    }


def _replace_user_messages_for_ai(
    messages: list,
    replacements: list[tuple[str, dict]],
) -> list:
    if not replacements:
        return list(messages)

    messages_for_ai = list(messages)
    search_end = len(messages_for_ai) - 1
    for persisted_content, runtime_message in reversed(replacements):
        for index in range(search_end, -1, -1):
            message = messages_for_ai[index]
            if not isinstance(message, dict):
                continue
            if (
                message.get("role") == "user"
                and message.get("content") == persisted_content
            ):
                messages_for_ai[index] = runtime_message
                search_end = index - 1
                break

    return messages_for_ai
