import asyncio
import logging
import re
from typing import Any, Awaitable, Callable

import telegram.error

from core.telegram_utils import (
    PartialTelegramSendError,
    retry_telegram_send,
    safe_send_markdown,
    split_ai_reply,
    telegram_error_summary,
)
from .tools.sticker_tools import choose_sticker_file_id, sticker_exists

AsyncSendFunc = Callable[..., Awaitable[Any]]
MAX_STICKERS_PER_REPLY = 10

_STICKER_DIRECTIVE_RE = re.compile(
    r"^\[sticker_pack:(?P<pack>[A-Za-z0-9_]+)\s+emoji:(?P<emoji>[^\]]+)\]$"
)


class PartialAIReplySendError(Exception):
    def __init__(
        self,
        message: str,
        sent_messages: list[Any],
        sent_content: str,
    ) -> None:
        super().__init__(message)
        self.sent_messages = list(sent_messages)
        self.sent_content = sent_content


def _parse_sticker_directive(line: str) -> tuple[str, str] | None:
    match = _STICKER_DIRECTIVE_RE.match(line.strip())
    if not match:
        return None
    pack_name = match.group("pack").strip()
    emoji = match.group("emoji").strip()
    if not pack_name or not emoji:
        return None
    return pack_name, emoji


async def normalize_sticker_directives(
    text: str,
    *,
    logger: logging.Logger,
) -> str:
    """Downgrade invalid sticker directives to their plain emoji text."""
    normalized_segments: list[str] = []

    for segment in split_ai_reply(str(text)):
        normalized_lines: list[str] = []
        in_code_block = False

        for line in segment.splitlines():
            stripped = line.strip()
            if stripped.startswith("```"):
                in_code_block = not in_code_block
                normalized_lines.append(line)
                continue

            directive = None if in_code_block else _parse_sticker_directive(stripped)
            if directive is None:
                normalized_lines.append(line)
                continue

            pack_name, emoji = directive
            exists = await asyncio.to_thread(sticker_exists, pack_name, emoji)
            if exists:
                normalized_lines.append(line)
                continue

            if logger:
                logger.info(
                    "Invalid sticker directive downgraded to emoji: pack=%s emoji=%s",
                    pack_name,
                    emoji,
                )
            normalized_lines.append(emoji)

        normalized_segments.append("\n".join(normalized_lines).strip())

    return "\n\n".join(segment for segment in normalized_segments if segment).strip()


async def send_ai_reply_with_stickers(
    *,
    bot: Any,
    chat_id: int,
    text: str,
    first_text_send: AsyncSendFunc,
    fallback_send: AsyncSendFunc,
    logger: logging.Logger,
    reply_to_message_id: int | None = None,
) -> list[Any]:
    """Send an AI reply, interpreting sticker directives as Telegram stickers."""
    if not str(text).strip():
        logger.info("Skipping empty AI reply send: chat_id=%s", chat_id)
        return []

    sent_messages: list[Any] = []
    sent_content_parts: list[str] = []
    text_has_been_sent = False
    sticker_count = 0

    def sent_content() -> str:
        return "\n\n".join(part for part in sent_content_parts if part).strip()

    async def flush_text(lines: list[str]) -> None:
        nonlocal text_has_been_sent
        payload = "\n".join(lines).strip()
        lines.clear()
        if not payload:
            return

        send_func = first_text_send if not text_has_been_sent else fallback_send
        try:
            results = await safe_send_markdown(
                send_func,
                payload,
                logger=logger,
                fallback_send=fallback_send,
            )
        except PartialTelegramSendError as exc:
            sent_messages.extend(exc.sent_messages)
            if exc.sent_text.strip():
                sent_content_parts.append(exc.sent_text.strip())
                text_has_been_sent = True
            raise
        sent_messages.extend(results)
        sent_content_parts.append(payload)
        text_has_been_sent = True

    async def send_sticker(pack_name: str, emoji: str) -> None:
        nonlocal sticker_count, text_has_been_sent
        if sticker_count >= MAX_STICKERS_PER_REPLY:
            return

        file_id = await asyncio.to_thread(choose_sticker_file_id, pack_name, emoji)
        if not file_id:
            await flush_text([emoji])
            return

        send_kwargs: dict[str, Any] = {}
        if not text_has_been_sent and reply_to_message_id is not None:
            send_kwargs["reply_to_message_id"] = reply_to_message_id
            send_kwargs["allow_sending_without_reply"] = True

        try:
            sent_message = await retry_telegram_send(
                lambda: bot.send_sticker(
                    chat_id=chat_id,
                    sticker=file_id,
                    **send_kwargs,
                ),
                logger=logger,
                action="send AI sticker",
            )
        except telegram.error.BadRequest as exc:
            if "message to be replied not found" not in str(exc).lower():
                logger.warning(
                    "Failed to send sticker pack=%s emoji=%s: %s",
                    pack_name,
                    emoji,
                    exc,
                )
                await flush_text([emoji])
                return
            try:
                sent_message = await retry_telegram_send(
                    lambda: bot.send_sticker(
                        chat_id=chat_id,
                        sticker=file_id,
                    ),
                    logger=logger,
                    action="send AI sticker without reply",
                )
            except Exception as fallback_exc:
                logger.warning(
                    "Failed to send sticker without reply pack=%s emoji=%s: %s",
                    pack_name,
                    emoji,
                    telegram_error_summary(fallback_exc),
                )
                await flush_text([emoji])
                return
        except Exception as exc:
            logger.warning(
                "Failed to send sticker pack=%s emoji=%s after retry: %s",
                pack_name,
                emoji,
                telegram_error_summary(exc),
            )
            await flush_text([emoji])
            return

        sent_messages.append(sent_message)
        sent_content_parts.append(f"[sticker_pack:{pack_name} emoji:{emoji}]")
        sticker_count += 1
        text_has_been_sent = True

    try:
        for segment in split_ai_reply(str(text)):
            buffer: list[str] = []
            in_code_block = False

            for line in segment.splitlines():
                stripped = line.strip()
                if stripped.startswith("```"):
                    in_code_block = not in_code_block
                    buffer.append(line)
                    continue

                directive = None if in_code_block else _parse_sticker_directive(stripped)
                if directive is None:
                    buffer.append(line)
                    continue

                await flush_text(buffer)
                await send_sticker(*directive)

            await flush_text(buffer)
    except Exception as exc:
        if sent_messages:
            raise PartialAIReplySendError(
                str(exc),
                sent_messages,
                sent_content(),
            ) from exc
        raise

    return sent_messages
