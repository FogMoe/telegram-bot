import asyncio
import logging
from types import SimpleNamespace

import telegram.error

from core import telegram_utils
from features.ai import sticker_sender


def test_send_ai_reply_with_stickers_retries_sticker_timeout(monkeypatch):
    sleeps = []

    class FakeBot:
        def __init__(self):
            self.sticker_calls = 0

        async def send_sticker(self, **kwargs):
            self.sticker_calls += 1
            if self.sticker_calls == 1:
                raise telegram.error.TimedOut("Timed out")
            return SimpleNamespace(message_id=321)

    async def fake_sleep(delay):
        sleeps.append(delay)

    async def fail_text_send(*args, **kwargs):
        raise AssertionError("text fallback should not be used")

    bot = FakeBot()
    monkeypatch.setattr(telegram_utils.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        sticker_sender,
        "choose_sticker_file_id",
        lambda pack_name, emoji: "sticker-file-id",
    )

    sent = asyncio.run(
        sticker_sender.send_ai_reply_with_stickers(
            bot=bot,
            chat_id=123,
            text="[sticker_pack:test_pack emoji:smile]",
            first_text_send=fail_text_send,
            fallback_send=fail_text_send,
            logger=logging.getLogger(__name__),
        )
    )

    assert len(sent) == 1
    assert bot.sticker_calls == 2
    assert sleeps == [telegram_utils.TELEGRAM_SEND_RETRY_INITIAL_DELAY_SECONDS]


def test_send_ai_reply_with_stickers_recognizes_embedded_directive(monkeypatch):
    events = []

    class FakeBot:
        async def send_sticker(self, **kwargs):
            events.append(("sticker", kwargs["sticker"]))
            return SimpleNamespace(message_id=321)

    async def send_text(text, **kwargs):
        events.append(("text", text))
        return SimpleNamespace(message_id=len(events))

    monkeypatch.setattr(
        sticker_sender,
        "choose_sticker_file_id",
        lambda pack_name, emoji: "sticker-file-id",
    )

    sent = asyncio.run(
        sticker_sender.send_ai_reply_with_stickers(
            bot=FakeBot(),
            chat_id=123,
            text="before[sticker_pack:test_pack emoji:smile]after",
            first_text_send=send_text,
            fallback_send=send_text,
            logger=logging.getLogger(__name__),
        )
    )

    assert len(sent) == 3
    assert events == [
        ("text", "before"),
        ("sticker", "sticker-file-id"),
        ("text", "after"),
    ]


def test_normalize_sticker_directives_downgrades_embedded_invalid_directive(monkeypatch):
    monkeypatch.setattr(
        sticker_sender,
        "sticker_exists",
        lambda pack_name, emoji: False,
    )

    normalized = asyncio.run(
        sticker_sender.normalize_sticker_directives(
            "before[sticker_pack:missing_pack emoji:🙂]after",
            logger=logging.getLogger(__name__),
        )
    )

    assert normalized == "before🙂after"


def test_normalize_sticker_directives_preserves_embedded_valid_directive(monkeypatch):
    monkeypatch.setattr(
        sticker_sender,
        "sticker_exists",
        lambda pack_name, emoji: True,
    )
    text = "before[sticker_pack:test_pack emoji:🙂]after"

    normalized = asyncio.run(
        sticker_sender.normalize_sticker_directives(
            text,
            logger=logging.getLogger(__name__),
        )
    )

    assert normalized == text
