import asyncio
import logging

import pytest

from features.ai import telegram_visible_sender


class _Bot:
    async def send_chat_action(self, **kwargs):
        return None


async def _unused_send(*args, **kwargs):
    return None


def _make_handler():
    return telegram_visible_sender.TelegramVisibleContentHandler(
        bot=_Bot(),
        chat_id=123,
        first_text_send=_unused_send,
        fallback_send=_unused_send,
        logger=logging.getLogger(__name__),
    )


def test_no_response_sentinel_is_not_sent_as_visible_content(monkeypatch):
    sent_texts = []

    async def fake_send_ai_reply_with_stickers(**kwargs):
        sent_texts.append(kwargs["text"])
        return ["sent_message"]

    monkeypatch.setattr(
        telegram_visible_sender,
        "send_ai_reply_with_stickers",
        fake_send_ai_reply_with_stickers,
    )

    async def run_test():
        handler = _make_handler()

        result = await handler._send("  [NO_RESPONSE]  ")

        assert result == ""
        assert sent_texts == []
        assert handler.sent_contents == []
        assert handler.sent_count == 0

    asyncio.run(run_test())


def test_no_response_sentinel_is_removed_before_visible_content_is_sent(monkeypatch):
    sent_texts = []

    async def fake_send_ai_reply_with_stickers(**kwargs):
        sent_texts.append(kwargs["text"])
        return ["sent_message"]

    monkeypatch.setattr(
        telegram_visible_sender,
        "send_ai_reply_with_stickers",
        fake_send_ai_reply_with_stickers,
    )

    async def run_test():
        handler = _make_handler()

        result = await handler._send("hello [NO_RESPONSE] world")

        assert result == "hello  world"
        assert sent_texts == ["hello  world"]
        assert handler.sent_contents == ["hello  world"]
        assert handler.sent_count == 1

    asyncio.run(run_test())


def test_a_lease_lost_while_the_send_is_being_prepared_still_stops_the_delivery(monkeypatch):
    import threading

    from features.ai.types import JobAbortedError

    sent_texts = []
    event = threading.Event()

    async def fake_normalize(text, *, logger):
        event.set()  # 贴纸校验期间租约丢失
        return text

    async def fake_send_ai_reply_with_stickers(**kwargs):
        sent_texts.append(kwargs["text"])
        return ["sent_message"]

    monkeypatch.setattr(telegram_visible_sender, "normalize_sticker_directives", fake_normalize)
    monkeypatch.setattr(
        telegram_visible_sender, "send_ai_reply_with_stickers", fake_send_ai_reply_with_stickers
    )

    async def run_test():
        handler = telegram_visible_sender.TelegramVisibleContentHandler(
            bot=_Bot(),
            chat_id=123,
            first_text_send=_unused_send,
            fallback_send=_unused_send,
            logger=logging.getLogger(__name__),
            abort_event=event,
        )
        with pytest.raises(JobAbortedError):
            await handler("你好")
        assert handler.sent_count == 0

    asyncio.run(run_test())

    assert sent_texts == []
