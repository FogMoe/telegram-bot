"""对话入口的准入：全局容量、每用户待处理数、等待阈值与过载提示，全部发生在扣费之前。

整轮对话本身用替身（`run_turn`），这里只关心「这一轮有没有开始」以及用户收到了什么。
"""

import asyncio
import time
from types import SimpleNamespace

import pytest

from core import admission, command_cooldown, metrics
from core.deadline import Deadline
from features.ai import conversation_locks
from features.conversation import batching, handlers, lifecycle

BOT_ID = 4242


def make_message(message_id, text="你好", *, reply_to_bot=False, replies=None):
    async def reply_text(text, **kwargs):
        if replies is not None:
            replies.append((message_id, text))

    reply = SimpleNamespace(from_user=SimpleNamespace(id=BOT_ID)) if reply_to_bot else None
    return SimpleNamespace(
        message_id=message_id,
        text=text,
        caption=None,
        date=None,
        reply_to_message=reply,
        reply_text=reply_text,
    )


def make_update(message, *, user_id=7, chat_type="private", chat_id=None):
    return SimpleNamespace(
        update_id=None,
        message=message,
        edited_message=None,
        effective_chat=SimpleNamespace(
            id=chat_id if chat_id is not None else user_id,
            type=chat_type,
            title="群" if chat_type != "private" else None,
        ),
        effective_user=SimpleNamespace(id=user_id, username="kc", first_name="K", language_code="zh"),
    )


def queued(update):
    return batching._QueuedUpdate(update=update, context=SimpleNamespace(bot=object()))


@pytest.fixture
def world(monkeypatch, settings_override):
    """替换整轮对话、冷却与群聊历史；默认配置下限制很宽松，各测试自己收紧。"""
    state = SimpleNamespace(turns=[], group_logs=[], turn_seconds=0.0, notices=[])
    metrics.REGISTRY.reset()
    admission.reset_admission()
    monkeypatch.setattr(conversation_locks, "_CONVERSATION_LOCKS", {})

    async def fake_run_turn(request, services=None, settings=None):
        state.turns.append(request)
        await asyncio.sleep(state.turn_seconds)

    async def fake_cooldown(update):
        return True

    async def fake_log_group_message(message, chat_id, **kwargs):
        state.group_logs.append((message.message_id, chat_id))

    monkeypatch.setattr(handlers, "run_turn", fake_run_turn)
    monkeypatch.setattr(command_cooldown, "check_chat_cooldown", fake_cooldown)
    monkeypatch.setattr(handlers.group_chat_history, "log_group_message", fake_log_group_message)
    monkeypatch.setattr(lifecycle, "_BOT_ID", BOT_ID)
    monkeypatch.setattr(lifecycle, "_BOT_USERNAME", "FogMoeBot")
    monkeypatch.setattr(handlers.triggers.config, "AI_DIRECT_TRIGGER_PHRASES", ["雾萌"])
    yield state
    admission.reset_admission()
    metrics.REGISTRY.reset()


def send(user_id, message, *, chat_type="private", chat_id=None):
    update = make_update(message, user_id=user_id, chat_type=chat_type, chat_id=chat_id)
    return handlers._reply_locked((update.effective_chat.id, user_id), [queued(update)])


def test_requests_within_capacity_start_without_waiting(world):
    async def scenario():
        await asyncio.gather(send(1, make_message(1)), send(2, make_message(2)))

    asyncio.run(scenario())

    assert len(world.turns) == 2
    assert all(request.queue_seconds < 0.05 for request in world.turns)
    assert all(isinstance(request.deadline, Deadline) for request in world.turns)


def test_the_turn_deadline_comes_from_the_configuration(world, settings_override):
    settings_override(CHAT_TURN_DEADLINE_SECONDS=123)

    asyncio.run(send(1, make_message(1)))

    (request,) = world.turns
    assert request.deadline is not None
    assert 122 < request.deadline.remaining() <= 123


@pytest.mark.slow
def test_a_request_over_the_global_limit_queues_and_reports_the_wait(world, settings_override):
    settings_override(CHAT_MAX_CONCURRENT_TURNS=1, CHAT_QUEUE_MAX_WAIT_SECONDS=5)
    world.turn_seconds = 0.1

    async def scenario():
        first = asyncio.create_task(send(1, make_message(1)))
        await asyncio.sleep(0.02)
        second = asyncio.create_task(send(2, make_message(2)))
        await asyncio.gather(first, second)

    asyncio.run(scenario())

    assert [request.sender.user_id for request in world.turns] == [1, 2]
    assert world.turns[0].queue_seconds < 0.05
    assert world.turns[1].queue_seconds >= 0.07  # 排队时间交给了这一轮


@pytest.mark.slow
def test_waiting_longer_than_the_threshold_gets_a_busy_notice_and_never_starts_the_turn(
    world, settings_override
):
    settings_override(CHAT_MAX_CONCURRENT_TURNS=1, CHAT_QUEUE_MAX_WAIT_SECONDS=0.05)
    world.turn_seconds = 0.4
    replies = []

    async def scenario():
        first = asyncio.create_task(send(1, make_message(1)))
        await asyncio.sleep(0.02)
        started = time.monotonic()
        await send(2, make_message(22, replies=replies))
        rejected_after = time.monotonic() - started
        await first
        return rejected_after

    rejected_after = asyncio.run(scenario())

    # 被拒绝的请求没有进入一轮对话：扣费发生在一轮对话里，所以一定没有扣费。
    assert [request.sender.user_id for request in world.turns] == [1]
    assert replies == [(22, handlers.BUSY_TEXT)]
    assert "没有扣除硬币" in handlers.BUSY_TEXT and "not charged" in handlers.BUSY_TEXT
    assert rejected_after < 0.3
    assert metrics.snapshot().counter("admission.rejected", reason="queue_timeout") == 1


@pytest.mark.slow
def test_a_full_queue_answers_busy_immediately(world, settings_override):
    settings_override(
        CHAT_MAX_CONCURRENT_TURNS=1, CHAT_MAX_QUEUED_TURNS=1, CHAT_QUEUE_MAX_WAIT_SECONDS=5
    )
    world.turn_seconds = 0.3
    replies = []

    async def scenario():
        first = asyncio.create_task(send(1, make_message(1)))
        await asyncio.sleep(0.02)
        second = asyncio.create_task(send(2, make_message(2)))
        await asyncio.sleep(0.02)
        started = time.monotonic()
        await send(3, make_message(33, replies=replies))
        elapsed = time.monotonic() - started
        await asyncio.gather(first, second)
        return elapsed

    elapsed = asyncio.run(scenario())

    assert replies == [(33, handlers.BUSY_TEXT)]
    assert elapsed < 0.2
    assert [request.sender.user_id for request in world.turns] == [1, 2]


@pytest.mark.slow
def test_the_per_user_limit_rejects_the_overflow_behind_the_conversation_lock(
    world, settings_override
):
    settings_override(CHAT_MAX_PENDING_PER_USER=2)
    world.turn_seconds = 0.2
    replies = []

    async def scenario():
        first = asyncio.create_task(send(7, make_message(1)))
        await asyncio.sleep(0.02)
        second = asyncio.create_task(send(7, make_message(2)))  # 在会话锁后面等待
        await asyncio.sleep(0.02)
        await send(7, make_message(3, replies=replies))  # 第三条：待处理已满
        await asyncio.gather(first, second)

    asyncio.run(scenario())

    assert [request.messages[0].message.message_id for request in world.turns] == [1, 2]
    assert replies == [(3, handlers.USER_BUSY_TEXT)]
    assert metrics.snapshot().counter("admission.rejected", reason="user_limit") == 1


@pytest.mark.slow
def test_the_per_user_limit_does_not_affect_other_users(world, settings_override):
    settings_override(CHAT_MAX_PENDING_PER_USER=1)
    world.turn_seconds = 0.1

    async def scenario():
        await asyncio.gather(send(1, make_message(1)), send(2, make_message(2)))

    asyncio.run(scenario())

    assert len(world.turns) == 2


@pytest.mark.slow
def test_group_chatter_that_does_not_address_the_bot_is_neither_counted_nor_rejected(
    world, settings_override
):
    settings_override(CHAT_MAX_PENDING_PER_USER=1)
    world.turn_seconds = 0.2
    replies = []

    async def scenario():
        ai_turn = asyncio.create_task(
            send(7, make_message(1, "雾萌在吗"), chat_type="group", chat_id=-100)
        )
        await asyncio.sleep(0.02)
        # 同一个用户的闲聊：不唤起 AI，不占名额，也不会收到「繁忙」提示。
        chatter = asyncio.create_task(
            send(7, make_message(2, "随便聊聊", replies=replies), chat_type="group", chat_id=-100)
        )
        await asyncio.gather(ai_turn, chatter)

    asyncio.run(scenario())

    assert replies == []
    assert [request.messages[0].message.message_id for request in world.turns] == [1]
    assert sorted(world.group_logs) == [(1, -100), (2, -100)]


@pytest.mark.slow
def test_a_rejected_group_request_is_still_recorded_in_the_group_history(world, settings_override):
    settings_override(CHAT_MAX_PENDING_PER_USER=1)
    world.turn_seconds = 0.2
    replies = []

    async def scenario():
        first = asyncio.create_task(
            send(7, make_message(1, "雾萌在吗"), chat_type="group", chat_id=-100)
        )
        await asyncio.sleep(0.02)
        await send(
            7,
            make_message(2, "雾萌再说一句", replies=replies),
            chat_type="group",
            chat_id=-100,
        )
        await first

    asyncio.run(scenario())

    assert replies == [(2, handlers.USER_BUSY_TEXT)]
    assert (2, -100) in world.group_logs  # 被拒绝的消息仍然进入群聊上下文
    assert [request.messages[0].message.message_id for request in world.turns] == [1]


def test_a_turn_stuck_behind_the_lock_is_rejected_when_its_deadline_passes(world, monkeypatch):
    monkeypatch.setattr(
        handlers.AdmissionSettings,
        "from_config",
        classmethod(lambda cls, source=None: admission.AdmissionSettings(turn_deadline_seconds=0.05)),
    )
    replies = []

    async def scenario():
        lock = conversation_locks.get_conversation_lock(7)
        await lock.acquire()  # 同一用户之前的一轮一直不结束
        try:
            started = time.monotonic()
            await send(7, make_message(5, replies=replies))
            return time.monotonic() - started
        finally:
            lock.release()

    elapsed = asyncio.run(scenario())

    assert world.turns == []
    assert replies == [(5, handlers.BUSY_TEXT)]
    assert elapsed < 1.0
    assert metrics.snapshot().counter("admission.rejected", reason="deadline") == 1
    assert conversation_locks.get_conversation_lock(7).locked() is False


def test_a_closed_admission_answers_that_the_bot_is_restarting(world):
    replies = []

    async def scenario():
        admission.get_admission().close()
        await send(7, make_message(9, replies=replies))

    asyncio.run(scenario())

    assert world.turns == []
    assert replies == [(9, handlers.SHUTTING_DOWN_TEXT)]


@pytest.mark.slow
def test_a_failing_notice_never_breaks_the_handler(world, settings_override):
    settings_override(CHAT_MAX_CONCURRENT_TURNS=1, CHAT_QUEUE_MAX_WAIT_SECONDS=0)
    world.turn_seconds = 0.2

    async def broken_reply(text, **kwargs):
        raise RuntimeError("telegram is down")

    broken = make_message(2)
    broken.reply_text = broken_reply

    async def scenario():
        first = asyncio.create_task(send(1, make_message(1)))
        await asyncio.sleep(0.02)
        await send(2, broken)
        await first

    asyncio.run(scenario())

    assert [request.sender.user_id for request in world.turns] == [1]


@pytest.mark.slow
def test_slots_and_user_names_are_released_after_every_outcome(world, settings_override):
    settings_override(CHAT_MAX_CONCURRENT_TURNS=1, CHAT_QUEUE_MAX_WAIT_SECONDS=0)
    world.turn_seconds = 0.05

    async def scenario():
        first = asyncio.create_task(send(1, make_message(1)))
        await asyncio.sleep(0.01)
        await send(2, make_message(2))  # 被拒绝
        await first
        await send(3, make_message(3))  # 之后仍然可以正常进入

    asyncio.run(scenario())

    controller = admission.get_admission()
    assert controller.running == 0 and controller.queued == 0
    assert controller.pending_for(1) == controller.pending_for(2) == controller.pending_for(3) == 0
    assert [request.sender.user_id for request in world.turns] == [1, 3]
