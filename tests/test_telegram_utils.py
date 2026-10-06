import asyncio
import logging
import warnings
from datetime import timedelta

import pytest
import telegram.error
from telegram.warnings import PTBDeprecationWarning

from fogmoe_telegram_bot.core import telegram_utils


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


def test_send_markdown_entities_sends_rendered_text_with_entities():
    calls = []

    async def fake_send(text, **kwargs):
        calls.append((text, kwargs))
        return object()

    asyncio.run(
        telegram_utils.send_markdown_entities(
            fake_send,
            "**注意**：`user_id` 和 snake_case_name",
            reply_to_message_id=7,
        )
    )

    [(text, kwargs)] = calls
    assert text == "注意：user_id 和 snake_case_name"
    assert "parse_mode" not in kwargs
    assert kwargs["reply_to_message_id"] == 7
    assert [(e.type, e.offset, e.length) for e in kwargs["entities"]] == [
        (telegram.MessageEntity.BOLD, 0, 2),
        (telegram.MessageEntity.CODE, 3, 7),
    ]


def test_send_markdown_entities_falls_back_to_rendered_plain_text():
    calls = []

    async def fake_send(text, **kwargs):
        calls.append((text, kwargs))
        if "entities" in kwargs:
            raise telegram.error.BadRequest("Can't parse entities")
        return object()

    asyncio.run(telegram_utils.send_markdown_entities(fake_send, "**粗体**"))

    assert [(text, "entities" in kwargs) for text, kwargs in calls] == [
        ("粗体", True),
        ("粗体", False),
    ]


def test_send_markdown_entities_splits_long_text_and_reports_partial_send():
    calls = []

    async def fake_send(text, **kwargs):
        calls.append((text, kwargs))
        if len(calls) > 1:
            raise telegram.error.BadRequest("Message is too long")
        return object()

    markdown = "\n".join(f"第{i}行 **粗体** " + "字" * 40 for i in range(120))

    with pytest.raises(telegram_utils.PartialTelegramSendError) as exc_info:
        asyncio.run(
            telegram_utils.send_markdown_entities(
                fake_send,
                markdown,
                reply_to_message_id=7,
            )
        )

    first_text, first_kwargs = calls[0]
    assert first_kwargs["reply_to_message_id"] == 7
    assert all("reply_to_message_id" not in kwargs for _, kwargs in calls[1:])
    assert all(
        len(text.encode("utf-16-le")) // 2 <= telegram_utils.TELEGRAM_MAX_MESSAGE_LENGTH
        for text, _ in calls
    )
    assert "**" not in first_text
    assert exc_info.value.sent_text == first_text.strip()
    assert len(exc_info.value.sent_messages) == 1


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
