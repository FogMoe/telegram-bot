"""管理员一键封禁与看原文：回调数据、权限、群里与私聊里按按钮的效果、加群消息与被删原文的记录（不连网络、不连数据库）。"""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest

from fogmoe_telegram_bot.core import telegram_history
from fogmoe_telegram_bot.features.moderation import admin_ban, spam_strikes
from fogmoe_telegram_bot.features.moderation import admin_ban_views as views
from fogmoe_telegram_bot.features.moderation.operations import deleted_messages, join_messages
from fogmoe_telegram_bot.features.moderation.operations import member_verify as verification

CHAT_ID = -100123
SPAMMER_ID = 7
ADMIN_ID = 1


class Recorder:
    def __init__(self, result=None, errors=()):
        self.calls = []
        self._result = result
        self._errors = list(errors)

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self._errors:
            error = self._errors.pop(0)
            if error is not None:
                raise error
        return self._result


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setattr(join_messages, "JOINS", join_messages.JoinMessages())
    monkeypatch.setattr(deleted_messages, "ORIGINALS", deleted_messages.DeletedMessages())
    monkeypatch.setattr(spam_strikes.spam_ai_operations, "forget_member", Recorder())
    monkeypatch.setattr(verification, "find_round_message", Recorder(result=None))
    monkeypatch.setattr(verification, "claim_round", Recorder(result=True))


def admin(status="administrator", restrict=True):
    return SimpleNamespace(status=status, can_restrict_members=restrict)


class FakeBot:
    def __init__(self, *, clicker=None, target_status="member", ban_error=None, delete_error=None, unban_error=None):
        self.clicker = clicker or admin()
        self.target = SimpleNamespace(status=target_status)  # 被封禁的人在群里的状态
        self.ban_error = ban_error
        self.delete_error = delete_error
        self.unban_error = unban_error
        self.bans = []
        self.unbans = []
        self.deleted = []
        self.group_posts = []

    async def get_chat_member(self, chat_id, user_id):
        await asyncio.sleep(0)  # 让同时到达的回调交错执行
        return self.clicker if user_id == ADMIN_ID else self.target

    async def ban_chat_member(self, chat_id, user_id, until_date=None):
        if self.ban_error:
            raise self.ban_error
        self.bans.append((chat_id, user_id, until_date))
        # 普通群没有封禁名单，被移出的人只是不在群里
        self.target.status = "kicked" if str(chat_id).startswith("-100") else "left"

    async def unban_chat_member(self, chat_id, user_id, only_if_banned=False):
        if self.unban_error:
            raise self.unban_error
        self.unbans.append((chat_id, user_id, only_if_banned))
        self.target.status = "left"

    async def delete_message(self, chat_id, message_id):
        if self.delete_error:
            raise self.delete_error
        self.deleted.append(message_id)

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.group_posts.append(text)


def markup_with(*datas, link=None):
    row = [InlineKeyboardButton("查看消息", url=link)] if link else []
    row += [InlineKeyboardButton(data, callback_data=data) for data in datas]
    return InlineKeyboardMarkup([row])


def make_query(data, *, chat_type="supergroup", message_id=90, markup=None, text_html="⚠️ 注意: 警告", answer=None):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=ADMIN_ID, mention_html=lambda: "<b>管理员</b>"),
        message=SimpleNamespace(
            message_id=message_id,
            chat=SimpleNamespace(type=chat_type),
            text_html=text_html,
            reply_markup=markup or markup_with(data),
        ),
        answer=answer or Recorder(),
        edit_message_text=Recorder(),
        edit_message_reply_markup=Recorder(),
    )


def callback(bot, query):
    return admin_ban.admin_ban_callback(SimpleNamespace(callback_query=query), SimpleNamespace(bot=bot))


def press(bot, data, **fields):
    query = make_query(data, **fields)
    asyncio.run(callback(bot, query))
    return query


def edited(query):
    (text,), kwargs = query.edit_message_text.calls[0]
    return text, kwargs["reply_markup"]


# ---------------------------------------------------------------------------
# 展示
# ---------------------------------------------------------------------------


def test_callback_data_round_trips_and_rejects_garbage():
    assert views.parse_action(views.ban_data(CHAT_ID, 7, 55)) == views.AdminAction("ban", CHAT_ID, 7, message_id=55)
    assert views.parse_action(views.ban_data(CHAT_ID, 7)) == views.AdminAction("ban", CHAT_ID, 7)
    assert views.parse_action(views.unban_data(CHAT_ID, 7, 1_800_000_000)) == views.AdminAction(
        "unban", CHAT_ID, 7, until=1_800_000_000
    )
    assert views.parse_action(views.view_data(CHAT_ID, 55)) == views.AdminAction("view", CHAT_ID, 0, message_id=55)
    for data in (
        None,
        "mod:ban:x:1:2",
        "mod:kick:1:2:0",
        "mod:unban:1:2",
        "mod:view:x:1",
        "mod:view:1",
        "mod:view:1:2:3",
        "verify_7",
        "rpt:ban:1:2:3",
    ):
        assert views.parse_action(data) is None
    assert len(views.ban_data(-1001234567890, 2**40, 2**31)) <= 64
    assert len(views.unban_data(-1001234567890, 2**40, 1_800_000_000)) <= 64
    assert len(views.view_data(-1001234567890, 2**31)) <= 64


def test_mentions_in_the_group_never_show_the_name():
    assert views.neutral_mention(7) == '<a href="tg://user?id=7">该用户</a>'
    assert views.neutral_mention(7, "新成员") == '<a href="tg://user?id=7">新成员</a>'


def test_join_messages_are_forgotten_once_taken_or_too_old():
    now = [0.0]
    joins = join_messages.JoinMessages(clock=lambda: now[0])
    joins.record(CHAT_ID, 7, 30)
    joins.record(CHAT_ID, 8, 31)

    assert joins.pop(CHAT_ID, 7) == 30
    assert joins.pop(CHAT_ID, 7) is None
    now[0] = join_messages.KEEP_SECONDS
    assert joins.pop(CHAT_ID, 8) is None


def test_originals_can_be_viewed_again_until_they_expire():
    now = [0.0]
    originals = deleted_messages.DeletedMessages(clock=lambda: now[0])
    originals.record(CHAT_ID, 55, "加我领福利")

    assert originals.get(CHAT_ID, 55) == "加我领福利"
    assert originals.get(CHAT_ID, 55) == "加我领福利"
    assert originals.get(CHAT_ID, 56) is None
    now[0] = deleted_messages.KEEP_SECONDS
    assert originals.get(CHAT_ID, 55) is None


def test_a_full_store_drops_only_the_oldest_originals(monkeypatch):
    monkeypatch.setattr(deleted_messages, "MAX_ENTRIES", 2)
    now = [0.0]
    originals = deleted_messages.DeletedMessages(clock=lambda: now[0])
    for message_id in (55, 56):
        originals.record(CHAT_ID, message_id, f"广告 {message_id}")
    originals.record(CHAT_ID, 55, "改过的广告 55")  # 重新记下的算最新的

    originals.record(CHAT_ID, 57, "广告 57")

    assert originals.get(CHAT_ID, 56) is None
    assert originals.get(CHAT_ID, 55) == "改过的广告 55"
    assert originals.get(CHAT_ID, 57) == "广告 57"


def test_originals_are_clipped_to_fit_the_popup():
    assert deleted_messages.clip("广" * 200) == "广" * 200
    clipped = deleted_messages.clip("广" * 300)
    assert clipped == "广" * 199 + "…" and len(clipped) == deleted_messages.POPUP_LIMIT
    # Telegram 按 UTF-16 计数，emoji 占两个位置
    assert deleted_messages.clip("😀" * 150) == "😀" * 99 + "…"


# ---------------------------------------------------------------------------
# 谁能点
# ---------------------------------------------------------------------------


def test_buttons_that_are_not_on_the_message_are_ignored():
    bot = FakeBot()

    query = press(bot, views.ban_data(CHAT_ID, 42), markup=markup_with(views.ban_data(CHAT_ID, SPAMMER_ID)))

    assert query.answer.calls == [((views.STALE_BUTTON,), {"show_alert": True})]
    assert bot.bans == []


@pytest.mark.parametrize("data", [views.ban_data(CHAT_ID, SPAMMER_ID), views.view_data(CHAT_ID, 55)], ids=["ban", "view"])
@pytest.mark.parametrize(
    "clicker",
    [admin(restrict=False), admin(status="member"), admin(status="restricted")],
    ids=["admin-without-ban-rights", "member", "restricted"],
)
def test_only_the_owner_or_admins_who_can_ban_may_press(clicker, data):
    deleted_messages.ORIGINALS.record(CHAT_ID, 55, "加我领福利")
    bot = FakeBot(clicker=clicker)

    query = press(bot, data)

    assert query.answer.calls == [((views.NOT_ALLOWED,), {"show_alert": True})]
    assert bot.bans == [] and query.edit_message_text.calls == []


# ---------------------------------------------------------------------------
# 群里的按钮
# ---------------------------------------------------------------------------


def test_banning_from_a_warning_is_permanent_and_rewrites_the_warning():
    join_messages.JOINS.record(CHAT_ID, SPAMMER_ID, 30)
    bot = FakeBot(clicker=admin(status="creator", restrict=False))

    query = press(bot, views.ban_data(CHAT_ID, SPAMMER_ID))

    assert bot.bans == [(CHAT_ID, SPAMMER_ID, None)]  # 永久封禁
    assert bot.deleted == [30]  # 加群消息
    assert bot.group_posts == []  # 改写警告本身，不另发公告
    text, keyboard = edited(query)
    assert text == "⚠️ 注意: 警告\n\n🚫 已被 <b>管理员</b> 永久封禁。"
    (button,), = keyboard.inline_keyboard
    assert (button.text, button.callback_data) == ("解除封禁", views.unban_data(CHAT_ID, SPAMMER_ID))


def test_banning_from_the_verification_message_ends_the_round(monkeypatch):
    monkeypatch.setattr(verification, "find_round_message", Recorder(result=90))
    claim = Recorder(result=True)
    monkeypatch.setattr(verification, "claim_round", claim)
    bot = FakeBot()

    query = press(bot, views.ban_data(CHAT_ID, SPAMMER_ID), message_id=90, text_html="欢迎 新成员 加入群组！")

    assert claim.calls == [((SPAMMER_ID, CHAT_ID, 90), {})]
    text, _ = edited(query)
    assert text == f'🚫 <a href="tg://user?id={SPAMMER_ID}">新成员</a> 已被 <b>管理员</b> 永久封禁。'


def test_basic_groups_only_promise_a_removal():
    # 普通群不执行封禁，被移出的人凭邀请链接就能回来
    bot = FakeBot()

    query = press(FakeBot(), views.ban_data(-4567, SPAMMER_ID))
    dm = press(bot, views.ban_data(-4567, SPAMMER_ID, 55), chat_type="private")

    assert edited(query)[0].endswith("🚫 已被 <b>管理员</b> 移出群组。")
    assert edited(dm)[0].endswith("✅ <b>管理员</b> 已删除消息并移出群组。")
    assert "永久封禁" not in bot.group_posts[0]


def test_viewing_the_original_shows_it_only_to_the_admin_who_pressed():
    deleted_messages.ORIGINALS.record(CHAT_ID, 55, "加我领福利")
    bot = FakeBot()

    query = press(bot, views.view_data(CHAT_ID, 55))

    assert query.answer.calls == [(("加我领福利",), {"show_alert": True})]
    assert query.edit_message_text.calls == [] and bot.group_posts == [] and bot.bans == []


def test_the_original_never_reaches_the_admins_ai_history():
    # 弹框平时会记进点按钮的人的 AI 对话记录，原文不能借此存下来
    deleted_messages.ORIGINALS.record(CHAT_ID, 55, "加我领福利")
    suppressed = []

    async def answer(*args, **kwargs):
        suppressed.append(telegram_history._CAPTURE_SUPPRESSED.get())

    press(FakeBot(), views.view_data(CHAT_ID, 55), answer=answer)

    assert suppressed == [True]
    assert telegram_history._CAPTURE_SUPPRESSED.get() is False


@pytest.mark.parametrize(("original", "popup"), [(None, views.ORIGINAL_GONE), ("", views.ORIGINAL_EMPTY)], ids=["gone", "empty"])
def test_an_original_that_is_gone_or_empty_says_so(original, popup):
    if original is not None:
        deleted_messages.ORIGINALS.record(CHAT_ID, 55, original)

    query = press(FakeBot(), views.view_data(CHAT_ID, 55))

    assert query.answer.calls == [((popup,), {"show_alert": True})]
    assert f"{deleted_messages.KEEP_SECONDS // 3600} 小时" in views.ORIGINAL_GONE


def test_the_original_button_stays_after_banning_and_unbanning():
    bot = FakeBot()

    banned = press(bot, views.ban_data(CHAT_ID, SPAMMER_ID), markup=views.warning_keyboard(CHAT_ID, 55, SPAMMER_ID))
    _, keyboard = edited(banned)
    assert [button.text for button in keyboard.inline_keyboard[0]] == [views.VIEW_LABEL, "解除封禁"]

    unbanned = press(bot, views.unban_data(CHAT_ID, SPAMMER_ID), markup=keyboard)
    _, keyboard = edited(unbanned)
    assert [button.callback_data for row in keyboard.inline_keyboard for button in row] == [views.view_data(CHAT_ID, 55)]


def test_a_failed_ban_keeps_the_buttons():
    bot = FakeBot(ban_error=BadRequest("Not enough rights to restrict/ban chat member"))

    query = press(bot, views.ban_data(CHAT_ID, SPAMMER_ID))

    assert query.answer.calls == [((views.BAN_FAILED,), {"show_alert": True})]
    assert query.edit_message_text.calls == []


# ---------------------------------------------------------------------------
# 举报私聊里的按钮
# ---------------------------------------------------------------------------


def test_banning_from_a_report_notice_deletes_the_message_and_announces():
    bot = FakeBot()

    query = press(bot, views.ban_data(CHAT_ID, SPAMMER_ID, 55), chat_type="private", text_html="📣 <b>新举报</b>")

    assert bot.deleted == [55]
    assert bot.bans == [(CHAT_ID, SPAMMER_ID, None)]
    assert bot.group_posts == [
        f'🚫 <a href="tg://user?id={SPAMMER_ID}">该用户</a> 的消息被管理员确认为垃圾信息，已被移出群组并永久封禁。'
    ]
    text, keyboard = edited(query)
    assert text == "📣 <b>新举报</b>\n\n✅ <b>管理员</b> 已删除消息并永久封禁。"
    assert keyboard.inline_keyboard[0][0].text == "解除封禁"


def test_a_report_ban_that_could_not_delete_says_so():
    bot = FakeBot(delete_error=BadRequest("Message can't be deleted"))

    query = press(bot, views.ban_data(CHAT_ID, SPAMMER_ID, 55), chat_type="private")

    text, _ = edited(query)
    assert bot.bans and "已永久封禁，但消息没能删除，请手动删除" in text


# ---------------------------------------------------------------------------
# 已经封禁过的人
# ---------------------------------------------------------------------------


def test_a_second_report_notice_for_a_banned_user_only_deletes_its_message():
    # 每位管理员、每条被举报的消息各有一条私聊，按钮都还在
    bot = FakeBot()
    press(bot, views.ban_data(CHAT_ID, SPAMMER_ID, 55), chat_type="private")
    data = views.ban_data(CHAT_ID, SPAMMER_ID, 56)

    query = press(
        bot, data, chat_type="private", text_html="📣 <b>新举报</b>", markup=markup_with(data, link="https://t.me/c/123/56")
    )

    assert len(bot.bans) == 1 and len(bot.group_posts) == 1  # 不再封禁，群里不再发公告
    assert bot.deleted == [55, 56]
    text, keyboard = edited(query)
    assert text == "📣 <b>新举报</b>\n\nℹ️ 对方此前已被封禁，没有重复处理；被举报的消息已删除。"
    assert keyboard is None


def test_a_report_notice_for_a_banned_user_keeps_the_link_when_the_message_stays():
    bot = FakeBot(target_status="kicked", delete_error=BadRequest("Message can't be deleted"))
    data = views.ban_data(CHAT_ID, SPAMMER_ID, 55)

    query = press(bot, data, chat_type="private", markup=markup_with(data, link="https://t.me/c/123/55"))

    text, keyboard = edited(query)
    assert bot.bans == [] and bot.group_posts == []
    assert text.endswith("⚠️ 对方此前已被封禁，但被举报的消息没能删除，请手动删除。")
    assert [button.text for button in keyboard.inline_keyboard[0]] == ["查看消息"]


def test_a_warning_about_a_banned_user_only_loses_its_ban_button():
    bot = FakeBot(target_status="kicked")

    query = press(bot, views.ban_data(CHAT_ID, SPAMMER_ID), markup=views.warning_keyboard(CHAT_ID, 55, SPAMMER_ID))

    assert query.answer.calls == [(("对方此前已被封禁，不用重复操作。",), {"show_alert": True})]
    assert bot.bans == [] and query.edit_message_text.calls == []
    (), kwargs = query.edit_message_reply_markup.calls[0]
    assert [button.text for button in kwargs["reply_markup"].inline_keyboard[0]] == [views.VIEW_LABEL]


def test_someone_who_left_is_still_banned_except_in_basic_groups():
    # 超级群里发完广告就退群的人照样封禁，免得再回来；普通群没有封禁名单，不在群里就没什么可做
    supergroup, basic = FakeBot(target_status="left"), FakeBot(target_status="left")

    press(supergroup, views.ban_data(CHAT_ID, SPAMMER_ID, 55), chat_type="private")
    query = press(basic, views.ban_data(-4567, SPAMMER_ID, 55), chat_type="private")

    assert supergroup.bans == [(CHAT_ID, SPAMMER_ID, None)]
    assert basic.bans == [] and basic.group_posts == [] and basic.deleted == [55]
    assert edited(query)[0].endswith("ℹ️ 对方已经不在群里，没有重复处理；被举报的消息已删除。")


def test_two_presses_at_once_ban_and_announce_once():
    bot = FakeBot()
    data = views.ban_data(CHAT_ID, SPAMMER_ID, 55)
    first, second = make_query(data, chat_type="private"), make_query(data, chat_type="private")

    async def both():
        await asyncio.gather(callback(bot, first), callback(bot, second))

    asyncio.run(both())

    assert len(bot.bans) == 1 and len(bot.group_posts) == 1
    assert second.answer.calls == [((views.BAN_IN_PROGRESS,), {})]
    assert admin_ban._BANNING == set()


def test_a_ban_that_failed_can_be_tried_again():
    bot = FakeBot(ban_error=BadRequest("Chat not found"))
    press(bot, views.ban_data(CHAT_ID, SPAMMER_ID))
    bot.ban_error = None

    query = press(bot, views.ban_data(CHAT_ID, SPAMMER_ID))

    assert bot.bans == [(CHAT_ID, SPAMMER_ID, None)] and edited(query)[0].endswith("永久封禁。")


# ---------------------------------------------------------------------------
# 解除封禁
# ---------------------------------------------------------------------------


def test_unbanning_lifts_the_ban_and_removes_the_buttons():
    bot = FakeBot()

    query = press(bot, views.unban_data(CHAT_ID, SPAMMER_ID), text_html="🚫 已封禁")

    assert bot.unbans == [(CHAT_ID, SPAMMER_ID, True)]
    text, keyboard = edited(query)
    assert text == "🚫 已封禁\n\n✅ <b>管理员</b> 已解除封禁，对方可以重新加入群组。" and keyboard is None


def test_an_expired_unban_button_removes_only_itself():
    data = views.unban_data(CHAT_ID, SPAMMER_ID, int(datetime.now(UTC).timestamp()) - 60)
    bot = FakeBot()

    query = press(bot, data, markup=markup_with(data, link="https://t.me/c/123/55"))

    assert query.answer.calls == [((views.UNBAN_EXPIRED,), {"show_alert": True})]
    (), kwargs = query.edit_message_reply_markup.calls[0]
    assert [button.text for button in kwargs["reply_markup"].inline_keyboard[0]] == ["查看消息"]
    assert bot.unbans == []


def test_a_failed_unban_keeps_the_buttons():
    bot = FakeBot(unban_error=BadRequest("Chat not found"))

    query = press(bot, views.unban_data(CHAT_ID, SPAMMER_ID))

    assert query.answer.calls == [((views.UNBAN_FAILED,), {"show_alert": True})]
    assert query.edit_message_text.calls == []
