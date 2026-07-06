import asyncio
import logging

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
