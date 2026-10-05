"""对话的 Telegram 入口：准入判断与输入映射，不涉及一轮对话本身（见 test_conversation_turn.py）。"""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from fogmoe_telegram_bot.core import command_cooldown
from fogmoe_telegram_bot.features.ai import conversation_locks
from fogmoe_telegram_bot.features.conversation import batching, handlers, lifecycle

BOT_ID = 4242


def make_message(message_id, text, *, date=None, reply_to_bot=False):
    reply = SimpleNamespace(from_user=SimpleNamespace(id=BOT_ID)) if reply_to_bot else None
    return SimpleNamespace(
        message_id=message_id,
        text=text,
        caption=None,
        date=date,
        reply_to_message=reply,
    )


def make_update(message, *, chat_type="private", edited=False, update_id=None, chat_id=100):
    return SimpleNamespace(
        update_id=update_id,
        message=None if edited else message,
        edited_message=message if edited else None,
        effective_chat=SimpleNamespace(id=chat_id, type=chat_type, title="群" if chat_type != "private" else None),
        effective_user=SimpleNamespace(id=7, username="kc", first_name="K", language_code="zh"),
    )


def queued(update, bot):
    return batching._QueuedUpdate(update=update, context=SimpleNamespace(bot=bot))


@pytest.fixture
def pipeline(monkeypatch):
    """替换冷却、群聊历史与整轮对话，记录入口把什么交给了业务操作。"""
    state = SimpleNamespace(
        turns=[],
        group_logs=[],
        cooldown_allows=True,
    )

    async def fake_run_turn(request, services=None, settings=None):
        state.turns.append((request, services, settings))

    async def fake_cooldown(update):
        return state.cooldown_allows

    async def fake_log_group_message(message, chat_id, **kwargs):
        state.group_logs.append((message.message_id, chat_id))

    monkeypatch.setattr(handlers, "run_turn", fake_run_turn)
    monkeypatch.setattr(command_cooldown, "check_chat_cooldown", fake_cooldown)
    monkeypatch.setattr(handlers.group_chat_history, "log_group_message", fake_log_group_message)
    monkeypatch.setattr(lifecycle, "_BOT_ID", BOT_ID)
    monkeypatch.setattr(lifecycle, "_BOT_USERNAME", "FogMoeBot")
    monkeypatch.setattr(handlers.triggers.config, "AI_DIRECT_TRIGGER_PHRASES", ["雾萌"])
    return state


def drive(items, **kwargs):
    return asyncio.run(handlers._reply_batch_unlocked(items, **kwargs))


def test_a_private_batch_becomes_one_turn_request_sorted_by_time(pipeline):
    bot = object()
    early = datetime(2026, 10, 5, 8, 0, 0, tzinfo=timezone.utc)
    late = datetime(2026, 10, 5, 8, 0, 5, tzinfo=timezone.utc)
    items = [
        queued(make_update(make_message(12, "后发", date=late), update_id=902), bot),
        queued(make_update(make_message(11, "先发", date=early), update_id=901), bot),
    ]

    drive(items, queue_seconds=0.75)

    ((request, services, settings),) = pipeline.turns
    assert [item.message.message_id for item in request.messages] == [11, 12]
    assert [item.update_id for item in request.messages] == [901, 902]
    assert request.reply_target.message_id == 12
    assert request.chat.chat_id == 100 and request.chat.chat_type == "private"
    assert request.sender.user_id == 7 and request.sender.display_name == "kc"
    assert request.conversation_id == 7
    assert request.bot is bot
    assert request.queue_seconds == 0.75
    assert services is None and settings is None


def test_an_edited_message_is_marked_as_edited(pipeline):
    items = [queued(make_update(make_message(11, "改过", date=None), edited=True), object())]

    drive(items)

    ((request, _, _),) = pipeline.turns
    assert request.messages[0].edited is True


def test_explicit_services_and_settings_are_passed_through(pipeline):
    services, settings = object(), object()

    drive(
        [queued(make_update(make_message(1, "hi")), object())],
        services=services,
        settings=settings,
    )

    ((_, got_services, got_settings),) = pipeline.turns
    assert got_services is services and got_settings is settings


def test_a_user_in_chat_cooldown_never_reaches_the_turn(pipeline):
    pipeline.cooldown_allows = False

    drive([queued(make_update(make_message(1, "hi")), object())])

    assert pipeline.turns == []


def test_updates_without_a_message_are_skipped(pipeline):
    empty = SimpleNamespace(
        update_id=1,
        message=None,
        edited_message=None,
        effective_chat=SimpleNamespace(id=100, type="private", title=None),
        effective_user=SimpleNamespace(id=7),
    )

    drive([queued(empty, object())])
    drive([])

    assert pipeline.turns == []


def test_a_group_message_without_a_trigger_is_logged_but_not_answered(pipeline):
    items = [queued(make_update(make_message(1, "随便聊聊"), chat_type="supergroup"), object())]

    drive(items)

    assert pipeline.turns == []
    assert pipeline.group_logs == [(1, 100)]


@pytest.mark.parametrize(
    "message",
    [
        make_message(1, "雾萌在吗"),
        make_message(1, "是的", reply_to_bot=True),
        make_message(1, "你好 @fogmoebot"),
    ],
)
def test_a_group_message_that_addresses_the_bot_starts_a_turn(pipeline, message):
    drive([queued(make_update(message, chat_type="group"), object())])

    assert len(pipeline.turns) == 1
    assert pipeline.turns[0][0].chat.is_group
    assert pipeline.group_logs == [(1, 100)]


def test_one_triggering_message_makes_the_whole_group_batch_a_turn(pipeline):
    bot = object()
    items = [
        queued(make_update(make_message(1, "无关的话", date=None), chat_type="group"), bot),
        queued(make_update(make_message(2, "雾萌你好", date=None), chat_type="group"), bot),
    ]

    drive(items)

    ((request, _, _),) = pipeline.turns
    assert len(request.messages) == 2
    assert pipeline.group_logs == [(1, 100), (2, 100)]


def test_the_explicit_bot_command_is_not_logged_to_the_group_history_twice(pipeline):
    items = [queued(make_update(make_message(1, "/fogmoebot 你好"), chat_type="group"), object())]

    drive(items)

    assert pipeline.group_logs == []


def test_a_missing_bot_identity_is_refreshed_before_judging_a_group_reply(pipeline, monkeypatch):
    monkeypatch.setattr(lifecycle, "_BOT_ID", None)
    refreshed = []

    async def refresh(bot, *, source):
        refreshed.append(source)
        monkeypatch.setattr(lifecycle, "_BOT_ID", BOT_ID)
        return True

    monkeypatch.setattr(lifecycle, "_refresh_bot_identity", refresh)

    drive([queued(make_update(make_message(1, "是的", reply_to_bot=True), chat_type="group"), object())])

    assert refreshed == ["group message handling"]
    assert len(pipeline.turns) == 1


def test_waiting_for_the_conversation_lock_is_reported_as_queue_time(monkeypatch):
    seen = []

    async def fake_batch(items, *, queue_seconds=0.0, **kwargs):
        seen.append(queue_seconds)

    monkeypatch.setattr(handlers, "_reply_batch_unlocked", fake_batch)
    monkeypatch.setattr(conversation_locks, "_CONVERSATION_LOCKS", {})

    async def scenario():
        lock = conversation_locks.get_conversation_lock(7)
        await lock.acquire()
        waiting = asyncio.create_task(handlers._reply_locked((100, 7), []))
        await asyncio.sleep(0.05)
        lock.release()
        await waiting

    asyncio.run(scenario())

    assert seen and seen[0] >= 0.04
