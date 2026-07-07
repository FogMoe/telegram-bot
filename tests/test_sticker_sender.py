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
