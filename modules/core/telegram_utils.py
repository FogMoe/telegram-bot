"""Utility helpers for Telegram messages and sending."""

import logging
from io import BytesIO
from functools import partial
from typing import Any, Awaitable, Callable, Optional

import telegram.error
from telegram.constants import ParseMode

try:  # pragma: no cover - optional dependency
    import telegramify_markdown
except ImportError:  # pragma: no cover
    telegramify_markdown = None

AsyncSendFunc = Callable[..., Awaitable[Any]]

TELEGRAM_MAX_MESSAGE_LENGTH = 4096


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _message_context(
    message_type: str,
    *,
    text: str | None = None,
    caption: str | None = None,
    summary: str | None = None,
    emoji: str | None = None,
) -> dict[str, str | None]:
    return {
        "type": message_type,
        "text": text,
        "caption": caption,
        "summary": summary,
        "emoji": emoji,
    }


def describe_message_for_context(message: Any) -> dict[str, str | None]:
    """Return a compact, prompt-friendly description of a Telegram message."""
    if not message:
        return _message_context("other", summary="[unsupported message]")

    text = _optional_text(getattr(message, "text", None))
    if text:
        return _message_context("text", text=text)

    caption = _optional_text(getattr(message, "caption", None))

    if getattr(message, "photo", None):
        return _message_context(
            "photo",
            caption=caption,
            summary=None if caption else "[photo without caption]",
        )

    sticker = getattr(message, "sticker", None)
    if sticker:
        emoji = _optional_text(getattr(sticker, "emoji", None))
        summary = f"[sticker {emoji}]" if emoji else "[sticker]"
        return _message_context("sticker", summary=summary, emoji=emoji)

    document = getattr(message, "document", None)
    if document:
        file_name = _optional_text(getattr(document, "file_name", None))
        summary = f"[document: {file_name}]" if file_name else "[document]"
        return _message_context("document", caption=caption, summary=summary)

    if getattr(message, "animation", None):
        return _message_context(
            "animation",
            caption=caption,
            summary=None if caption else "[animation]",
        )

    if getattr(message, "video", None):
        return _message_context(
            "video",
            caption=caption,
            summary=None if caption else "[video message]",
        )

    audio = getattr(message, "audio", None)
    if audio:
        title = (
            _optional_text(getattr(audio, "title", None))
            or _optional_text(getattr(audio, "file_name", None))
        )
        summary = f"[audio: {title}]" if title else "[audio]"
        return _message_context("audio", caption=caption, summary=summary)

    if getattr(message, "voice", None):
        return _message_context("voice", caption=caption, summary="[voice message]")

    if getattr(message, "video_note", None):
        return _message_context("video_note", summary="[video note]")

    if getattr(message, "poll", None):
        question = _optional_text(getattr(message.poll, "question", None))
        summary = f"[poll: {question}]" if question else "[poll]"
        return _message_context("poll", summary=summary)

    if getattr(message, "location", None):
        return _message_context("location", summary="[location]")

    if getattr(message, "venue", None):
        title = _optional_text(getattr(message.venue, "title", None))
        summary = f"[venue: {title}]" if title else "[venue]"
        return _message_context("venue", summary=summary)

    if getattr(message, "contact", None):
        return _message_context("contact", summary="[contact]")

    if getattr(message, "dice", None):
        emoji = _optional_text(getattr(message.dice, "emoji", None))
        summary = f"[dice {emoji}]" if emoji else "[dice]"
        return _message_context("dice", summary=summary, emoji=emoji)

    if caption:
        return _message_context("other", caption=caption)

    return _message_context("other", summary="[unsupported message]")


def _split_text_segments(text: str, limit: int = TELEGRAM_MAX_MESSAGE_LENGTH) -> list[str]:
    """Split text into chunks that respect Telegram's message length limit."""
    if len(text) <= limit:
        return [text]

    segments: list[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= limit:
            segments.append(remaining)
            break

        split_idx = remaining.rfind("\n", 0, limit)
        if split_idx <= 0:
            split_idx = remaining.rfind(" ", 0, limit)
        if split_idx <= 0:
            split_idx = limit

        chunk = remaining[:split_idx]
        segments.append(chunk)
        remaining = remaining[split_idx:]
        if remaining.startswith("\n"):
            remaining = remaining[1:]

    return [segment for segment in segments if segment]


def split_ai_reply(text: str) -> list[str]:
    if not text or "\n\n" not in text:
        return [text]

    segments: list[str] = []
    in_code_block = False
    current: list[str] = []

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            current.append(line)
            continue

        if in_code_block:
            current.append(line)
            continue

        if not stripped:
            if current:
                segments.append("\n".join(current).strip())
                current = []
            continue
        current.append(line)

    if current:
        segments.append("\n".join(current).strip())

    return [segment for segment in segments if segment] or [text]


async def safe_send_markdown(
    send_func: AsyncSendFunc,
    text: str,
    *,
    parse_mode: str = ParseMode.MARKDOWN,
    logger: logging.Logger = logging.getLogger(__name__),
    fallback_send: Optional[AsyncSendFunc] = None,
    **kwargs: Any,
) -> list[Any]:
    """Send text using Telegram Markdown with graceful fallbacks.

    Args:
        send_func: Awaitable function that accepts ``text`` as first arg.
        text: Message content.
        parse_mode: Telegram parse mode to attempt first.
        logger: Logger for warning messages.
        **kwargs: Additional keyword arguments forwarded to ``send_func``.

    Returns:
        A list of Telegram API responses, one per sent chunk.
    """

    def _is_empty_text_error(message: str) -> bool:
        lower = message.lower()
        return (
            "message text is empty" in lower
            or "text must be non-empty" in lower
            or "text must be non empty" in lower
        )

    def _bad_request_info(error: telegram.error.BadRequest) -> dict:
        message = str(error)
        lower = message.lower()
        return {
            "text": message,
            "lower": lower,
            "missing_reply": "message to be replied not found" in lower,
            "empty_text": _is_empty_text_error(message),
        }

    async def _attempt_send(
        target: AsyncSendFunc,
        payload: str,
        *,
        mode: str | None,
        send_kwargs: dict[str, Any],
    ) -> Any:
        current_func = target
        attempted_fallback = False

        while True:
            call_kwargs = dict(send_kwargs)
            if current_func is fallback_send:
                call_kwargs.pop("reply_to_message_id", None)
                call_kwargs.pop("reply_to_message", None)
                call_kwargs.pop("quote", None)
            try:
                if mode is not None:
                    result = await current_func(payload, parse_mode=mode, **call_kwargs)
                else:
                    call_kwargs.pop("parse_mode", None)
                    result = await current_func(payload, **call_kwargs)
                return result
            except telegram.error.BadRequest as exc:
                info = _bad_request_info(exc)

                if info["empty_text"]:
                    payload = "雾萌娘不想回复你的这条消息。"
                    continue

                if (
                    not attempted_fallback
                    and fallback_send is not None
                    and info["missing_reply"]
                ):
                    current_func = fallback_send
                    attempted_fallback = True
                    continue
                raise
            except ValueError as exc:
                if _is_empty_text_error(str(exc)):
                    payload = "雾萌娘不想回复你的这条消息。"
                    continue
                raise

    async def _send_single_chunk(chunk_text: str, chunk_kwargs: dict[str, Any]) -> Any:
        try:
            return await _attempt_send(
                send_func,
                chunk_text,
                mode=parse_mode,
                send_kwargs=chunk_kwargs,
            )
        except telegram.error.BadRequest as exc:
            if logger:
                logger.warning("Markdown send failed (%s).", exc)

        if parse_mode == ParseMode.MARKDOWN and telegramify_markdown is not None:
            try:
                converted = telegramify_markdown.markdownify(
                    chunk_text,
                    max_line_length=None,
                    normalize_whitespace=False,
                )
                return await _attempt_send(
                    send_func,
                    converted,
                    mode=ParseMode.MARKDOWN_V2,
                    send_kwargs=chunk_kwargs,
                )
            except telegram.error.BadRequest as conv_exc:
                if logger:
                    logger.warning(
                        "MarkdownV2 retry failed (%s). Falling back to plain text.",
                        conv_exc,
                    )

        return await _attempt_send(
            send_func,
            chunk_text,
            mode=None,
            send_kwargs=chunk_kwargs,
        )

    chunks = _split_text_segments(text)

    results: list[Any] = []
    for index, chunk in enumerate(chunks):
        chunk_kwargs = dict(kwargs)
        if index > 0:
            chunk_kwargs.pop("reply_to_message_id", None)
            chunk_kwargs.pop("reply_to_message", None)
            chunk_kwargs.pop("quote", None)
        result = await _send_single_chunk(chunk, chunk_kwargs)
        results.append(result)

    return results


def partial_send(bot_method: AsyncSendFunc, /, *args: Any, **kwargs: Any) -> AsyncSendFunc:
    """Utility to pre-bind positional/keyword args for bot send methods."""
    return partial(bot_method, *args, **kwargs)


async def send_document_bytes(
    bot: Any,
    chat_id: int,
    content: bytes,
    filename: str,
    *,
    caption: str | None = None,
    logger: logging.Logger | None = None,
) -> bool:
    if not content:
        return False

    file_obj = BytesIO(content)
    file_obj.name = filename

    try:
        await bot.send_document(
            chat_id=chat_id,
            document=file_obj,
            filename=filename,
            caption=caption,
        )
        return True
    except Exception as exc:  # pragma: no cover - defensive logging
        if logger:
            logger.warning("Failed to send document to %s: %s", chat_id, exc)
        return False
