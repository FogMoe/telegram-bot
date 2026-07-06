import logging

from features.ai.tools import sticker_tools


def test_list_available_stickers_returns_minimal_model_payload(monkeypatch):
    monkeypatch.setattr(
        sticker_tools,
        "_load_pack_configs",
        lambda: {
            "PackA": {
                "name": "PackA",
                "summary": "Casual reactions.",
                "avoid": "Formal replies.",
            }
        },
    )
    monkeypatch.setattr(
        sticker_tools,
        "_metadata_for_pack",
        lambda name: {
            "name": name,
            "title": "Internal title",
            "summary": "Casual reactions.",
            "avoid": "Formal replies.",
            "sticker_type": "regular",
            "sticker_count": 3,
            "static_count": 3,
            "video_count": 0,
            "animated_count": 0,
            "emoji_to_file_ids": {
                "😊": ["file_1"],
                "😢": ["file_2", "file_3"],
            },
            "cached_at": 100,
            "expires_at": 200,
        },
    )

    result = sticker_tools.list_available_stickers_tool()

    assert result == {
        "packs": [
            {
                "name": "PackA",
                "title": "Internal title",
                "summary": "Casual reactions.",
                "avoid": "Formal replies.",
                "emojis": ["😢", "😊"],
            }
        ],
        "status": "available",
    }


def test_list_available_stickers_logs_unconfigured_pack_without_model_error(
    monkeypatch,
    caplog,
):
    monkeypatch.setattr(
        sticker_tools,
        "_load_pack_configs",
        lambda: {
            "PackA": {
                "name": "PackA",
                "summary": "",
                "avoid": "",
            }
        },
    )

    with caplog.at_level(logging.INFO, logger=sticker_tools.logger.name):
        result = sticker_tools.list_available_stickers_tool("MissingPack")

    assert result == {"packs": [], "status": "unavailable"}
    assert "MissingPack" in caplog.text
