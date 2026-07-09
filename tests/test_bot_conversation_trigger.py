from types import SimpleNamespace

from core import bot_conversation


def test_photo_caption_mention_triggers_group_ai_response(monkeypatch):
    monkeypatch.setattr(bot_conversation, "_BOT_USERNAME", "FogMoeBot")
    monkeypatch.setattr(bot_conversation.config, "AI_DIRECT_TRIGGER_PHRASES", [])
    message = SimpleNamespace(text=None, caption="看看这张图 @fogmoebot")

    assert bot_conversation._message_contains_direct_ai_trigger(message) is True


def test_photo_caption_configured_phrase_triggers_group_ai_response(monkeypatch):
    monkeypatch.setattr(bot_conversation, "_BOT_USERNAME", "FogMoeBot")
    monkeypatch.setattr(bot_conversation.config, "AI_DIRECT_TRIGGER_PHRASES", ["Bot Please"])
    message = SimpleNamespace(text=None, caption="bot please 看这张图")

    assert bot_conversation._message_contains_direct_ai_trigger(message) is True


def test_plain_photo_without_caption_does_not_trigger_group_ai_response(monkeypatch):
    monkeypatch.setattr(bot_conversation, "_BOT_USERNAME", "FogMoeBot")
    monkeypatch.setattr(bot_conversation.config, "AI_DIRECT_TRIGGER_PHRASES", [])
    message = SimpleNamespace(text=None, caption=None)

    assert bot_conversation._message_contains_direct_ai_trigger(message) is False
