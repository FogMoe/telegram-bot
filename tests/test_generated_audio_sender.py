import asyncio
import logging

import telegram.error

from core import telegram_utils
from features.ai import generated_audio_sender


def test_send_with_retry_prefers_telegram_voice(monkeypatch, tmp_path):
    path = tmp_path / "voice.ogg"
    path.write_bytes(b"audio")
    calls = []

    async def fake_send_voice_once(**kwargs):
        calls.append("voice")
        return object()

    async def fake_send_audio_once(**kwargs):
        calls.append("audio")
        return object()

    async def fake_send_document_once(**kwargs):
        calls.append("document")
        return object()

    monkeypatch.setattr(generated_audio_sender, "_send_voice_once", fake_send_voice_once)
    monkeypatch.setattr(generated_audio_sender, "_send_audio_once", fake_send_audio_once)
    monkeypatch.setattr(generated_audio_sender, "_send_document_once", fake_send_document_once)

    sent = asyncio.run(
        generated_audio_sender._send_with_retry(
            bot=object(),
            chat_id=123,
            path=path,
            filename="hello.ogg",
            logger=logging.getLogger(__name__),
        )
    )

    assert sent is not None
    assert calls == ["voice"]


def test_send_with_retry_falls_back_to_audio_after_voice_timeouts(monkeypatch, tmp_path):
    path = tmp_path / "voice.ogg"
    path.write_bytes(b"audio")
    calls = []
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    async def fake_send_voice_once(**kwargs):
        calls.append("voice")
        raise telegram.error.TimedOut("Timed out")

    async def fake_send_audio_once(**kwargs):
        calls.append("audio")
        return object()

    async def fake_send_document_once(**kwargs):
        calls.append("document")
        return object()

    monkeypatch.setattr(telegram_utils.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(generated_audio_sender, "_send_voice_once", fake_send_voice_once)
    monkeypatch.setattr(generated_audio_sender, "_send_audio_once", fake_send_audio_once)
    monkeypatch.setattr(generated_audio_sender, "_send_document_once", fake_send_document_once)

    sent = asyncio.run(
        generated_audio_sender._send_with_retry(
            bot=object(),
            chat_id=123,
            path=path,
            filename="hello.ogg",
            logger=logging.getLogger(__name__),
        )
    )

    assert sent is not None
    assert calls == ["voice", "voice", "voice", "audio"]
    assert sleeps == [
        telegram_utils.TELEGRAM_SEND_RETRY_INITIAL_DELAY_SECONDS,
        telegram_utils.TELEGRAM_SEND_RETRY_INITIAL_DELAY_SECONDS * 2,
    ]


def test_send_generated_audio_from_tool_logs_enforces_total_limit(monkeypatch, tmp_path):
    audio_ids = ["a1", "a2", "a3", "a4"]
    paths = {}
    for audio_id in audio_ids:
        path = tmp_path / f"{audio_id}.mp3"
        path.write_bytes(b"audio")
        paths[audio_id] = str(path)

    attempted_paths = []

    def fake_pop_generated_audio_file(audio_id):
        return paths.get(audio_id)

    async def fake_send_with_retry(**kwargs):
        attempted_paths.append(kwargs["path"])
        return object()

    monkeypatch.setattr(
        generated_audio_sender,
        "pop_generated_audio_file",
        fake_pop_generated_audio_file,
    )
    monkeypatch.setattr(generated_audio_sender, "_send_with_retry", fake_send_with_retry)

    tool_logs = [
        {
            "type": "tool_result",
            "tool_name": "generate_voice",
            "internal_result": {
                "status": "generated",
                "audios": [
                    {"audio_id": "a1", "filename": "a1.mp3"},
                    {"audio_id": "a2", "filename": "a2.mp3"},
                ],
            },
        },
        {
            "type": "tool_result",
            "tool_name": "generate_voice",
            "internal_result": {
                "status": "generated",
                "audios": [
                    {"audio_id": "a3", "filename": "a3.mp3"},
                    {"audio_id": "a4", "filename": "a4.mp3"},
                ],
            },
        },
    ]

    sent = asyncio.run(
        generated_audio_sender.send_generated_audio_from_tool_logs(
            bot=object(),
            chat_id=123,
            tool_logs=tool_logs,
            logger=logging.getLogger(__name__),
        )
    )

    assert len(sent) == generated_audio_sender.MAX_GENERATED_AUDIO_PER_REPLY
    assert [path.stem for path in attempted_paths] == ["a1", "a2", "a3"]
