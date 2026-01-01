"""Utility helpers for Telegram message sending."""

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
    if not text or "\n" not in text:
        return [text]

    segments: list[str] = []
    in_code_block = False
    code_buffer: list[str] = []

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_code_block:
                code_buffer.append(line)
                segments.append("\n".join(code_buffer))
                code_buffer = []
                in_code_block = False
            else:
                in_code_block = True
                code_buffer = [line]
            continue

        if in_code_block:
            code_buffer.append(line)
            continue

        if stripped:
            segments.append(line)

    if code_buffer:
        segments.append("\n".join(code_buffer))

    return segments or [text]


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

    def _bad_request_info(error: telegram.error.BadRequest) -> dict:
        message = str(error)
        lower = message.lower()
        return {
            "text": message,
            "lower": lower,
            "missing_reply": "message to be replied not found" in lower,
            "empty_text": "message text is empty" in lower,
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
                    payload = "雾萌娘没看到这条消息，请再发一次吧。"
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
