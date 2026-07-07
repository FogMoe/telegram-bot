import asyncio
import logging
import warnings
from datetime import timedelta

import pytest
import telegram.error
from telegram.warnings import PTBDeprecationWarning

from core import telegram_utils


def test_safe_send_markdown_does_not_replace_empty_text_errors(monkeypatch):
    attempted_payloads = []

    async def fake_send(text, **kwargs):
        attempted_payloads.append(text)
        raise telegram.error.BadRequest("Message text is empty")

    monkeypatch.setattr(telegram_utils, "telegramify_markdown", None)

    with pytest.raises(telegram.error.BadRequest):
        asyncio.run(telegram_utils.safe_send_markdown(fake_send, ""))

    assert "雾萌娘不想回复你的这条消息。" not in attempted_payloads


def test_safe_send_markdown_retries_timed_out(monkeypatch):
    attempts = []
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    async def fake_send(text, **kwargs):
        attempts.append((text, kwargs))
        if len(attempts) == 1:
            raise telegram.error.TimedOut("Timed out")
        return object()

    monkeypatch.setattr(telegram_utils.asyncio, "sleep", fake_sleep)

    sent = asyncio.run(
        telegram_utils.safe_send_markdown(
            fake_send,
            "hello",
            logger=logging.getLogger(__name__),
        )
    )

    assert len(sent) == 1
    assert len(attempts) == 2
    assert sleeps == [telegram_utils.TELEGRAM_SEND_RETRY_INITIAL_DELAY_SECONDS]


def test_retry_telegram_send_uses_retry_after_delay(monkeypatch):
    attempts = 0
    sleeps = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", PTBDeprecationWarning)
        retry_after_error = telegram.error.RetryAfter(timedelta(seconds=2))

    async def fake_sleep(delay):
        sleeps.append(delay)

    async def operation():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise retry_after_error
        return "ok"

    monkeypatch.setattr(telegram_utils.asyncio, "sleep", fake_sleep)

    result = asyncio.run(
        telegram_utils.retry_telegram_send(
            operation,
            logger=logging.getLogger(__name__),
            action="test send",
        )
    )

    assert result == "ok"
    assert attempts == 2
    assert sleeps == [2 + telegram_utils.TELEGRAM_RETRY_AFTER_PADDING_SECONDS]


def test_retry_telegram_send_does_not_retry_bad_request(monkeypatch):
    attempts = 0
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    async def operation():
        nonlocal attempts
        attempts += 1
        raise telegram.error.BadRequest("Message text is empty")

    monkeypatch.setattr(telegram_utils.asyncio, "sleep", fake_sleep)

    with pytest.raises(telegram.error.BadRequest):
        asyncio.run(
            telegram_utils.retry_telegram_send(
                operation,
                logger=logging.getLogger(__name__),
                action="test send",
            )
        )

    assert attempts == 1
    assert sleeps == []
