"""Guest Mode：发往召唤聊天的请求改写成同一条 guest 回复，guest 更新只回自我介绍、不往下传。"""

import asyncio
import logging
from types import SimpleNamespace

import pytest
from telegram import Chat, InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity, Update
from telegram.error import BadRequest
from telegram.ext import ApplicationHandlerStop, ExtBot, filters
from telegram.request import BaseRequest

from fogmoe_telegram_bot.core import guest_replies, telegram_history
from fogmoe_telegram_bot.core.guest_replies import (
    GuestModeUnsupported,
    GuestReplySession,
    guest_reply_scope,
)
from fogmoe_telegram_bot.core.telegram_history import HistoryTrackingExtBot
from fogmoe_telegram_bot.features.guest import handlers as guest

GROUP = Chat(id=-100123, type="supergroup", title="某群")


class NoNetwork(BaseRequest):
    """不建 httpx 客户端（每个要 0.4 秒）；请求都被下面的 `api` 夹具在 `_do_post` 截住。"""

    @property
    def read_timeout(self):
        return None

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, *args, **kwargs):
        raise AssertionError("tests must not reach the network")


@pytest.fixture(scope="module")
def bot():
    return HistoryTrackingExtBot("123:token", request=NoNetwork(), get_updates_request=NoNetwork())


@pytest.fixture
def api(monkeypatch):
    """替换真正发请求的 `ExtBot._do_post`，记下 guest 层之后实际发给 Telegram 的请求。"""
    calls = []
    failures = {}

    async def fake_do_post(self, endpoint, data, **kwargs):
        calls.append((endpoint, data))
        if endpoint in failures:
            raise failures[endpoint]
        if endpoint == "answerGuestQuery":
            return {"inline_message_id": "inline-1"}
        if endpoint == "sendMessage":
            return {
                "message_id": 1,
                "date": 0,
                "chat": {"id": data["chat_id"], "type": "private"},
                "text": data["text"],
            }
        return True

    async def skip_recording(bot, message):
        return None

    monkeypatch.setattr(ExtBot, "_do_post", fake_do_post)
    # 发出的消息不写历史，测试不碰数据库。
    monkeypatch.setattr(telegram_history, "_record_bot_message", skip_recording)
    return SimpleNamespace(calls=calls, failures=failures)


def run_in_guest_scope(operation):
    session = GuestReplySession(query_id="query-1", chat=GROUP)

    async def run():
        with guest_reply_scope(session):
            return await operation()

    return session, asyncio.run(run())


def rendered(call):
    """一次 answerGuestQuery 或 inline 编辑最终显示的 (text, entities, reply_markup)。"""
    endpoint, data = call
    if endpoint == "answerGuestQuery":
        result = data["result"]
        content = result.input_message_content
        return content.message_text, content.entities, result.reply_markup
    assert data["inline_message_id"] == "inline-1"
    return data["text"], data.get("entities"), data.get("reply_markup")


def test_the_first_message_answers_the_guest_query_and_comes_back_as_a_message(bot, api):
    session, sent = run_in_guest_scope(lambda: bot.send_message(GROUP.id, "你好"))

    assert [endpoint for endpoint, _ in api.calls] == ["answerGuestQuery"]
    assert api.calls[0][1]["guest_query_id"] == "query-1"
    assert rendered(api.calls[0])[0] == "你好"
    assert session.inline_message_id == "inline-1"
    assert sent.chat.id == GROUP.id
    assert sent.message_id >= 1_000_000_000
    assert sent.text == "你好"


def test_later_messages_are_appended_to_the_same_reply_with_their_entities_shifted(bot, api):
    bold = MessageEntity(MessageEntity.BOLD, offset=0, length=2)

    async def send_two():
        await bot.send_message(GROUP.id, "第一")
        await bot.send_message(GROUP.id, "第二", entities=[bold])

    run_in_guest_scope(send_two)

    assert [endpoint for endpoint, _ in api.calls] == ["answerGuestQuery", "editMessageText"]
    text, entities, _ = rendered(api.calls[1])
    assert text == "第一\n\n第二"
    assert [(entity.type, entity.offset, entity.length) for entity in entities] == [
        (MessageEntity.BOLD, 4, 2)
    ]


def test_editing_and_deleting_a_returned_message_changes_only_its_part(bot, api):
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("再来", callback_data="again")]])

    async def send_edit_delete():
        first = await bot.send_message(GROUP.id, "处理中")
        second = await bot.send_message(GROUP.id, "结果", reply_markup=keyboard)
        await first.edit_text("完成")
        await second.delete()

    session, _ = run_in_guest_scope(send_edit_delete)

    assert [rendered(call)[0] for call in api.calls] == [
        "处理中",
        "处理中\n\n结果",
        "完成\n\n结果",
        "完成",
    ]
    assert rendered(api.calls[2])[2] == keyboard
    assert rendered(api.calls[3])[2] is None
    assert [segment.text for segment in session.segments] == ["完成"]


def test_typing_is_skipped_and_other_methods_on_the_summoning_chat_are_refused(bot, api):
    async def act():
        assert await bot.send_chat_action(GROUP.id, "typing") is True
        with pytest.raises(GuestModeUnsupported):
            await bot.ban_chat_member(GROUP.id, 42)
        with pytest.raises(GuestModeUnsupported):
            await bot.delete_message(GROUP.id, 7)

    run_in_guest_scope(act)

    assert api.calls == []


def test_requests_to_other_chats_and_outside_the_scope_are_sent_normally(bot, api):
    run_in_guest_scope(lambda: bot.send_message(42, "私聊"))
    asyncio.run(bot.send_message(GROUP.id, "没有 guest 会话"))

    assert [(endpoint, data["chat_id"]) for endpoint, data in api.calls] == [
        ("sendMessage", 42),
        ("sendMessage", GROUP.id),
    ]


def test_a_failed_redraw_leaves_the_reply_as_it_was(bot, api):
    async def send_two():
        await bot.send_message(GROUP.id, "第一")
        api.failures["editMessageText"] = BadRequest("Message_too_long")
        with pytest.raises(BadRequest):
            await bot.send_message(GROUP.id, "第二")

    session, _ = run_in_guest_scope(send_two)

    assert [segment.text for segment in session.segments] == ["第一"]


def test_an_unchanged_redraw_is_not_an_error(bot, api):
    async def edit_to_same_text():
        sent = await bot.send_message(GROUP.id, "一样")
        api.failures["editMessageText"] = BadRequest("Message is not modified")
        await sent.edit_text("一样")

    run_in_guest_scope(edit_to_same_text)


def test_plain_text_drops_the_oldest_parts_once_the_reply_is_too_long(bot, api):
    async def send_long():
        await bot.send_message(GROUP.id, "a" * 3000)
        await bot.send_message(GROUP.id, "b" * 3000)

    run_in_guest_scope(send_long)

    assert rendered(api.calls[-1])[0] == "b" * 3000


def test_parts_with_different_formats_show_only_the_latest_one(bot, api):
    async def send_mixed():
        await bot.send_message(GROUP.id, "纯文本")
        await bot.send_message(GROUP.id, "<b>HTML</b>", parse_mode="HTML")

    run_in_guest_scope(send_mixed)

    assert api.calls[-1][1]["text"] == "<b>HTML</b>"
    assert api.calls[-1][1]["parse_mode"] == "HTML"


def guest_update(bot):
    return Update.de_json(
        {
            "update_id": 9,
            "guest_message": {
                "message_id": 7,
                "date": 0,
                "chat": GROUP.to_dict(),
                "from": {"id": 1, "is_bot": False, "first_name": "Alice"},
                "text": "@fogmoebot 你好",
                "guest_query_id": "query-9",
            },
        },
        bot,
    )


def test_a_guest_message_gets_the_introduction_and_goes_no_further(bot, api):
    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(guest.handle_guest_message(guest_update(bot), SimpleNamespace(bot=bot)))

    assert [endpoint for endpoint, _ in api.calls] == ["answerGuestQuery"]
    assert api.calls[0][1]["guest_query_id"] == "query-9"
    assert rendered(api.calls[0])[0] == guest.intro_text(None)
    assert guest_replies.current_guest_session() is None


def test_a_failed_guest_reply_is_logged_and_still_stops_the_update(bot, api, caplog):
    api.failures["answerGuestQuery"] = BadRequest("Query is too old")

    with caplog.at_level(logging.ERROR), pytest.raises(ApplicationHandlerStop):
        asyncio.run(guest.handle_guest_message(guest_update(bot), SimpleNamespace(bot=bot)))

    assert "guest 回复失败" in caplog.text


def test_the_guest_handler_takes_guest_updates_only(bot):
    handler_filter = filters.UpdateType.GUEST_MESSAGE
    message_update = Update.de_json(
        {
            "update_id": 10,
            "message": {"message_id": 1, "date": 0, "chat": GROUP.to_dict(), "text": "hi"},
        },
        bot,
    )

    assert handler_filter.check_update(guest_update(bot))
    assert not handler_filter.check_update(message_update)
