import asyncio

import pytest
import telegram.error

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
