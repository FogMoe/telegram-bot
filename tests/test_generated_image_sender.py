import asyncio
import logging

import telegram.error

from core import telegram_utils
from features.ai import generated_image_sender


def test_send_generated_image_uses_prompt_filename(monkeypatch, tmp_path):
    path = tmp_path / "local_temp.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n")
    recorded = {}

    def fake_pop_generated_image_file(image_id):
        return str(path)

    async def fake_send_with_retry(**kwargs):
        recorded.update(kwargs)
        return object()

    monkeypatch.setattr(
        generated_image_sender,
        "pop_generated_image_file",
        fake_pop_generated_image_file,
    )
    monkeypatch.setattr(generated_image_sender, "_send_with_retry", fake_send_with_retry)

    sent = asyncio.run(
        generated_image_sender.send_generated_images_from_tool_result(
            bot=object(),
            chat_id=123,
            result={
                "status": "generated",
                "image": {
                    "image_id": "image-1",
                    "filename": "draw a cat.png",
                },
            },
            logger=logging.getLogger(__name__),
        )
    )

    assert len(sent) == 1
    assert recorded["filename"] == "draw a cat.png"


def test_send_with_retry_retries_photo_timeout(monkeypatch, tmp_path):
    path = tmp_path / "local_temp.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n")
    calls = []
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    async def fake_send_photo_once(**kwargs):
        calls.append("photo")
        if len(calls) == 1:
            raise telegram.error.TimedOut("Timed out")
        return object()

    async def fake_send_document_once(**kwargs):
        calls.append("document")
        return object()

    monkeypatch.setattr(telegram_utils.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(generated_image_sender, "_send_photo_once", fake_send_photo_once)
    monkeypatch.setattr(generated_image_sender, "_send_document_once", fake_send_document_once)

    sent = asyncio.run(
        generated_image_sender._send_with_retry(
            bot=object(),
            chat_id=123,
            path=path,
            filename="hello.png",
            logger=logging.getLogger(__name__),
        )
    )

    assert sent is not None
    assert calls == ["photo", "photo"]
    assert sleeps == [telegram_utils.TELEGRAM_SEND_RETRY_INITIAL_DELAY_SECONDS]
