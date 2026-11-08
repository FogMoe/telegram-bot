"""用户消息、定时任务、空闲跟进三个入口共用的步骤：工具上下文、用户状态、回复整理、写历史、私聊回复。"""

import asyncio
import threading
from types import SimpleNamespace

from fogmoe_telegram_bot.core import telegram_history
from fogmoe_telegram_bot.features.ai import (
    history_writer,
    idle_followup,
    outbound,
    router,
    summary,
    user_state,
)
from fogmoe_telegram_bot.features.ai.tool_history import reply_record_entries
from fogmoe_telegram_bot.features.ai.tools.context import build_tool_context
from fogmoe_telegram_bot.features.ai.types import ABORT_EVENT_KEY

SECRET = "sk-test-0123456789abcdef"


# -- 工具上下文 ---------------------------------------------------------------------------


def test_tool_context_of_a_group_message_carries_the_chat_and_the_sender():
    context = build_tool_context(
        user_id=7,
        user_state_prompt="<user_state />",
        chat_id=-100,
        chat_type="supergroup",
        chat_title="群",
        message_id=9,
        message_edit_stamp=1700000000,
        username="kc",
        first_name="K",
        language_code="zh",
    )

    assert context == {
        "is_group": True,
        "group_id": -100,
        "chat_id": -100,
        "chat_type": "supergroup",
        "chat_title": "群",
        "message_id": 9,
        "message_edit_stamp": 1700000000,
        "user_id": 7,
        "username": "kc",
        "first_name": "K",
        "language_code": "zh",
        "user_state_prompt": "<user_state />",
    }


def test_tool_context_of_a_background_turn_has_no_chat_and_carries_the_abort_event():
    abort_event = threading.Event()

    context = build_tool_context(user_id=7, user_state_prompt="<user_state />", abort_event=abort_event)

    assert context["is_group"] is False
    assert context["group_id"] is None
    # 没有 chat_id：代执行命令在后台轮次里仍然不可用。
    assert context["chat_id"] is None and context["message_id"] is None
    assert context[ABORT_EVENT_KEY] is abort_event


# -- 用户状态提示词 ------------------------------------------------------------------------


def test_background_and_message_turns_build_the_same_user_state_prompt(monkeypatch):
    async def fake_fetch_one(sql, params):
        if "FROM user" in sql:
            return (2, 6, 4, " 喜欢茶 ")
        return (1,)

    async def fake_impression(user_id):
        return "很可爱\n"

    monkeypatch.setattr(user_state.mysql_connection, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(user_state.process_user, "async_get_user_impression", fake_impression)

    background = asyncio.run(user_state.build_user_state_prompt(7))
    message_turn = user_state.user_state_prompt(
        user_id=7,
        coins_free=6,
        coins_paid=4,
        permission=2,
        info=" 喜欢茶 ",
        record=user_state.UserStateRecord(impression="很可爱\n", diary_exists=True),
    )

    assert background == message_turn
    assert 'coins="10"' in background
    assert 'user_plan="paid"' in background
    assert 'diary_exists="true"' in background
    assert "<impression>很可爱</impression>" in background


def test_a_missing_user_has_no_user_state_prompt(monkeypatch):
    async def no_row(sql, params):
        return None

    monkeypatch.setattr(user_state.mysql_connection, "fetch_one", no_row)

    assert asyncio.run(user_state.build_user_state_prompt(404)) is None


# -- 回复整理 ---------------------------------------------------------------------------


def _record_stickers(seen):
    async def normalize(text):
        seen.append(text)
        return text.replace("[sticker]", "🙂")

    return normalize


def test_finish_reply_strips_the_no_response_marker_and_normalizes_stickers():
    seen = []

    reply = asyncio.run(outbound.finish_reply("你好 [sticker][no_response]", normalize_stickers=_record_stickers(seen)))

    assert reply == outbound.AIReply(text="你好 🙂", runtime_error=None)
    assert seen == ["你好 [sticker]"]


def test_finish_reply_recognises_fixed_error_notices():
    reply = asyncio.run(
        outbound.finish_reply(router.AI_SERVICE_ERROR_MESSAGE, normalize_stickers=_record_stickers([]))
    )

    assert reply.runtime_error == "all_ai_services_failed"


def test_an_empty_reply_skips_sticker_normalization():
    seen = []

    reply = asyncio.run(outbound.finish_reply("[no_response]", normalize_stickers=_record_stickers(seen)))

    assert reply == outbound.AIReply(text="", runtime_error=None)
    assert seen == []


# -- 写历史 ----------------------------------------------------------------------------


def test_the_final_reply_is_redacted_and_error_notices_are_not_written():
    ((role, content),) = reply_record_entries(f"key: api_key={SECRET}", None)

    assert role == "assistant"
    assert SECRET not in content
    assert reply_record_entries(router.AI_SERVICE_ERROR_MESSAGE, "all_ai_services_failed") == []
    assert reply_record_entries("  ", None) == []


def _patch_history(monkeypatch, insert_result, operations):
    async def fake_insert(conversation_id, records, **options):
        operations.append(("insert", conversation_id, list(records), options))
        return insert_result

    async def fake_archive(bot, user_id, records, *, logger):
        operations.append(("archive", user_id, records))

    async def fake_refresh(user_id):
        operations.append(("refresh_summary", user_id))

    monkeypatch.setattr(history_writer.mysql_connection, "async_insert_chat_records", fake_insert)
    monkeypatch.setattr(history_writer, "send_permanent_records_archive", fake_archive)
    monkeypatch.setattr(summary, "refresh_summary_after_overflow", fake_refresh)
    monkeypatch.setattr(
        summary,
        "schedule_summary_generation",
        lambda user_id: operations.append(("schedule_summary", user_id)),
    )


def test_an_overflowing_write_sends_the_archive_then_refreshes_the_summary(monkeypatch):
    operations = []
    _patch_history(monkeypatch, (True, "overflow", ["old"]), operations)

    asyncio.run(
        history_writer.write_history(
            7,
            [("user", "hi")],
            bot=object(),
            logger=SimpleNamespace(),
            system_prompt_extra="<user_state />",
        )
    )

    assert operations == [
        ("insert", 7, [("user", "hi")], {"system_prompt_extra": "<user_state />"}),
        ("archive", 7, ["old"]),
        ("refresh_summary", 7),
    ]


def test_a_new_snapshot_schedules_a_summary_and_an_empty_write_does_nothing(monkeypatch):
    operations = []
    _patch_history(monkeypatch, (True, None, []), operations)

    asyncio.run(history_writer.write_history(7, [("user", "hi")], bot=object(), logger=SimpleNamespace()))
    asyncio.run(history_writer.write_history(7, [], bot=object(), logger=SimpleNamespace()))

    assert [operation[0] for operation in operations] == ["insert", "schedule_summary"]


def test_the_idle_follow_up_writes_its_reply_redacted(monkeypatch):
    written = []

    async def fake_write_history(conversation_id, records, *, bot, logger):
        written.append((conversation_id, records))

    monkeypatch.setattr(idle_followup, "write_history", fake_write_history)
    claim = idle_followup.IdleFollowupClaim(user_id=7, activity_version=1, retry_count=0)

    asyncio.run(
        idle_followup._persist_completed_turn(
            claim,
            "<metadata origin=\"idle_recap\"/>",
            f"api_key={SECRET}",
            [],
            SimpleNamespace(bot=object()),
        )
    )

    ((conversation_id, records),) = written
    assert conversation_id == 7
    assert [role for role, _ in records] == ["user", "assistant"]
    assert SECRET not in records[1][1]


# -- 私聊回复 --------------------------------------------------------------------------


class FakeBot:
    def __init__(self):
        self.actions = []

    async def send_chat_action(self, **kwargs):
        self.actions.append(kwargs["action"])

    async def send_message(self, *args, **kwargs):
        return SimpleNamespace(message_id=1)


def _capture_sends(monkeypatch):
    sends = []

    async def fake_send(**kwargs):
        sends.append(
            {
                "text": kwargs["text"],
                "suppressed": telegram_history._CAPTURE_SUPPRESSED.get(),
            }
        )
        return [SimpleNamespace(message_id=1)]

    monkeypatch.setattr(outbound, "send_ai_reply_with_stickers", fake_send)
    return sends


def test_a_private_reply_is_sent_without_recording_it_again(monkeypatch):
    sends = _capture_sends(monkeypatch)
    bot = FakeBot()

    sent = asyncio.run(outbound.send_private_reply(bot=bot, user_id=7, text="hi", logger=SimpleNamespace()))

    assert len(sent) == 1
    assert sends == [{"text": "hi", "suppressed": True}]
    assert bot.actions == ["typing"]


def test_an_error_notice_is_sent_in_the_error_notice_scope(monkeypatch):
    sends = _capture_sends(monkeypatch)

    asyncio.run(
        outbound.send_private_reply(
            bot=FakeBot(),
            user_id=7,
            text=router.AI_SERVICE_ERROR_MESSAGE,
            runtime_error="all_ai_services_failed",
            logger=SimpleNamespace(),
        )
    )

    assert sends == [{"text": router.AI_SERVICE_ERROR_MESSAGE, "suppressed": False}]


def test_an_empty_private_reply_sends_nothing(monkeypatch):
    sends = _capture_sends(monkeypatch)
    bot = FakeBot()

    assert asyncio.run(outbound.send_private_reply(bot=bot, user_id=7, text=" ", logger=SimpleNamespace())) == []
    assert sends == [] and bot.actions == []
