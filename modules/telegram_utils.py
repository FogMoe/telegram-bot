"""Utility helpers for Telegram message sending."""

import logging
from functools import partial
from typing import Any, Awaitable, Callable, Optional

import telegram.error
from telegram.constants import ParseMode

try:  # pragma: no cover - optional dependency
    import telegramify_markdown
except ImportError:  # pragma: no cover
    telegramify_markdown = None

AsyncSendFunc = Callable[..., Awaitable[Any]]


async def safe_send_markdown(
    send_func: AsyncSendFunc,
    text: str,
    *,
    parse_mode: str = ParseMode.MARKDOWN,
    logger: logging.Logger = logging.getLogger(__name__),
    fallback_send: Optional[AsyncSendFunc] = None,
    **kwargs: Any,
) -> None:
    """Send text using Telegram Markdown with graceful fallbacks.

    Args:
        send_func: Awaitable function that accepts ``text`` as first arg.
        text: Message content.
        parse_mode: Telegram parse mode to attempt first.
        logger: Logger for warning messages.
        **kwargs: Additional keyword arguments forwarded to ``send_func``.
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

    async def _attempt_send(target: AsyncSendFunc, payload: str, *, mode: str | None) -> None:
        current_func = target
        attempted_fallback = False

        while True:
            send_kwargs = dict(kwargs)
            if current_func is fallback_send:
                send_kwargs.pop("reply_to_message_id", None)
                send_kwargs.pop("reply_to_message", None)
                send_kwargs.pop("quote", None)
            try:
                if mode is not None:
                    await current_func(payload, parse_mode=mode, **send_kwargs)
                else:
                    send_kwargs.pop("parse_mode", None)
                    await current_func(payload, **send_kwargs)
                return
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

    try:
        await _attempt_send(send_func, text, mode=parse_mode)
        return
    except telegram.error.BadRequest as exc:
        if logger:
            logger.warning("Markdown send failed (%s).", exc)
        markdown_exc = exc

    if parse_mode == ParseMode.MARKDOWN and telegramify_markdown is not None:
        try:
            converted = telegramify_markdown.markdownify(
                text,
                max_line_length=None,
                normalize_whitespace=False,
            )
            await _attempt_send(send_func, converted, mode=ParseMode.MARKDOWN_V2)
            return
        except telegram.error.BadRequest as conv_exc:
            if logger:
                logger.warning("MarkdownV2 retry failed (%s). Falling back to plain text.", conv_exc)

    await _attempt_send(send_func, text, mode=None)


def partial_send(bot_method: AsyncSendFunc, /, *args: Any, **kwargs: Any) -> AsyncSendFunc:
    """Utility to pre-bind positional/keyword args for bot send methods."""
    return partial(bot_method, *args, **kwargs)
