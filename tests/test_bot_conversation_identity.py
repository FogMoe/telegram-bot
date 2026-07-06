import asyncio
from types import SimpleNamespace

import telegram.error

from core import bot_conversation


def test_post_init_continues_when_bot_identity_fetch_has_network_error(monkeypatch):
    class FailingBot:
        async def get_me(self):
            raise telegram.error.NetworkError("httpx.ReadError: ")

    identity_calls = []
    loop_calls = []
    monkeypatch.setattr(bot_conversation, "_BOT_ID", None)
    monkeypatch.setattr(bot_conversation, "_BOT_USERNAME", "FogMoeBot")
    monkeypatch.setattr(
        bot_conversation.group_chat_history,
        "set_bot_identity",
        lambda user_id, username: identity_calls.append((user_id, username)),
    )
    monkeypatch.setattr(
        bot_conversation.db,
        "set_main_loop",
        lambda loop: loop_calls.append(loop),
    )

    asyncio.run(bot_conversation.post_init(SimpleNamespace(bot=FailingBot())))

    assert len(loop_calls) == 1
    assert identity_calls == []
    assert bot_conversation._BOT_ID is None
    assert bot_conversation._BOT_USERNAME == "FogMoeBot"


def test_post_init_caches_bot_identity_after_successful_fetch(monkeypatch):
    class SuccessfulBot:
        async def get_me(self):
            return SimpleNamespace(id=12345, username="ExampleBot")

    identity_calls = []
    monkeypatch.setattr(bot_conversation, "_BOT_ID", None)
    monkeypatch.setattr(bot_conversation, "_BOT_USERNAME", "FogMoeBot")
    monkeypatch.setattr(
        bot_conversation.group_chat_history,
        "set_bot_identity",
        lambda user_id, username: identity_calls.append((user_id, username)),
    )
    monkeypatch.setattr(bot_conversation.db, "set_main_loop", lambda loop: None)

    asyncio.run(bot_conversation.post_init(SimpleNamespace(bot=SuccessfulBot())))

    assert bot_conversation._BOT_ID == 12345
    assert bot_conversation._BOT_USERNAME == "ExampleBot"
    assert identity_calls == [(12345, "ExampleBot")]
