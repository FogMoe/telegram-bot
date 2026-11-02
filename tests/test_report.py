"""/report：票数与额度规则、举报流程、管理员私聊按钮（不连网络、不连数据库）。"""

import asyncio
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden

from fogmoe_telegram_bot.core import command_cooldown
from fogmoe_telegram_bot.features.moderation import report, report_views, spam_strikes
from fogmoe_telegram_bot.features.moderation.operations import report_votes as votes
from fogmoe_telegram_bot.features.moderation.operations.report_votes import Quota, Stage, VoteStatus
from fogmoe_telegram_bot.features.moderation.spam_ai import judge
from fogmoe_telegram_bot.features.moderation.spam_ai import operations as spam_ai_operations


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


CHAT_ID = -100123
TODAY = date(2026, 11, 2)


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    monkeypatch.setattr(votes, "BOOK", votes.ReportBook())
    monkeypatch.setattr(votes, "QUOTA", votes.DailyQuota())
    command_cooldown.command_cooldowns.clear()
    spam_ai_operations._daily_checks.clear()
    yield
    command_cooldown.command_cooldowns.clear()
    spam_ai_operations._daily_checks.clear()


# ---------------------------------------------------------------------------
# 规则
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("members", "required"),
    [(None, 5), (0, 1), (3, 1), (100, 1), (101, 2), (200, 2), (201, 3), (499, 5), (20_000, 5)],
)
def test_one_vote_per_hundred_members_capped_at_five(members, required):
    assert votes.required_votes(members) == required


def test_only_messages_from_the_last_day_can_be_reported():
    now = datetime(2026, 11, 2, 12, tzinfo=UTC)
    assert not votes.too_old(now - timedelta(hours=23), now)
    assert votes.too_old(now - timedelta(hours=25), now)


def test_each_person_votes_once_and_only_one_report_takes_the_judgment():
    book = votes.ReportBook()

    first = book.add(CHAT_ID, 55, 1)
    again = book.add(CHAT_ID, 55, 1)
    second = book.add(CHAT_ID, 55, 2)

    assert (first.status, first.count, first.first) == (VoteStatus.COUNTED, 1, True)
    assert (again.status, again.count) == (VoteStatus.DUPLICATE, 1)
    assert (second.count, second.first, second.stage) == (2, False, Stage.COLLECTING)
    assert book.claim(CHAT_ID, 55) is True
    assert book.claim(CHAT_ID, 55) is False
    assert book.add(CHAT_ID, 55, 3).stage is Stage.JUDGING
    book.release(CHAT_ID, 55)
    assert book.claim(CHAT_ID, 55) is True
    book.finish(CHAT_ID, 55)
    assert book.add(CHAT_ID, 55, 4).stage is Stage.DONE
    assert book.claim(CHAT_ID, 55) is False


def test_reports_are_forgotten_after_two_days():
    now = [0.0]
    book = votes.ReportBook(clock=lambda: now[0])
    book.add(CHAT_ID, 55, 1)
    book.claim(CHAT_ID, 55)
    book.finish(CHAT_ID, 55)

    now[0] = votes.RECORD_SECONDS
    vote = book.add(CHAT_ID, 55, 1)

    assert (vote.status, vote.first, vote.stage) == (VoteStatus.COUNTED, True, Stage.COLLECTING)


def test_unpaid_groups_have_a_per_group_and_a_shared_daily_quota(monkeypatch):
    monkeypatch.setattr(votes, "FREE_GROUP_DAILY_CHECKS", 2)
    monkeypatch.setattr(votes, "FREE_TOTAL_DAILY_CHECKS", 3)
    quota = votes.DailyQuota()

    results = [quota.take(-1, reporter, paid=False, today=TODAY) for reporter in (1, 2, 3)]
    other_group = [quota.take(-2, reporter, paid=False, today=TODAY) for reporter in (4, 5)]

    assert results == [Quota.OK, Quota.OK, Quota.GROUP_LIMIT]
    assert other_group == [Quota.OK, Quota.TOTAL_LIMIT]
    assert quota.take(-1, 1, paid=False, today=TODAY + timedelta(days=1)) is Quota.OK


def test_each_reporter_can_trigger_ten_checks_a_day():
    quota = votes.DailyQuota()

    results = [quota.take(-chat, 7, paid=False, today=TODAY) for chat in range(1, 12)]

    assert results[:10] == [Quota.OK] * 10
    assert results[10] is Quota.REPORTER_LIMIT
    # 额度不够时什么也没记：别的举报人在同一个群照常
    assert quota.take(-11, 8, paid=False, today=TODAY) is Quota.OK


def test_paid_groups_use_the_ai_check_quota(monkeypatch):
    taken = []

    def take_daily_check(chat_id, today=None):
        taken.append(chat_id)
        return len(taken) == 1

    monkeypatch.setattr(spam_ai_operations, "take_daily_check", take_daily_check)
    monkeypatch.setattr(votes, "FREE_GROUP_DAILY_CHECKS", 0)
    quota = votes.DailyQuota()

    assert quota.take(CHAT_ID, 1, paid=True, today=TODAY) is Quota.OK
    assert quota.take(CHAT_ID, 2, paid=True, today=TODAY) is Quota.GROUP_LIMIT
    assert taken == [CHAT_ID, CHAT_ID]


# ---------------------------------------------------------------------------
# 展示
# ---------------------------------------------------------------------------


def test_callback_data_round_trips_and_rejects_garbage():
    assert report_views.parse_action(report_views.ban_data(CHAT_ID, 55, 7)) == report_views.AdminAction(
        "ban", CHAT_ID, 7, 55
    )
    assert report_views.parse_action(report_views.unban_data(CHAT_ID, 7, 1_800_000_000)) == report_views.AdminAction(
        "unban", CHAT_ID, 7, until=1_800_000_000
    )
    for data in (None, "rpt:ban:x:1:2", "rpt:kick:1:2", "rpt:unban:1:2", "spam_help"):
        assert report_views.parse_action(data) is None
    assert len(report_views.ban_data(-1001234567890, 2**31, 2**40)) <= 64
    assert len(report_views.unban_data(-1001234567890, 2**40, 1_800_000_000)) <= 64


@pytest.mark.parametrize(
    ("chat_id", "username", "link"),
    [
        (-100123, "fogmoe", "https://t.me/fogmoe/55"),
        (-100123, None, "https://t.me/c/123/55"),
        (-4567, None, None),
    ],
)
def test_message_links_follow_the_group_kind(chat_id, username, link):
    reported = report_views.ReportedMessage(chat_id, "群", username, 55, 7, "小美", "hi")
    assert reported.link == link


# ---------------------------------------------------------------------------
# 举报流程
# ---------------------------------------------------------------------------


def user(user_id, name="小美", *, is_bot=False):
    return SimpleNamespace(
        id=user_id,
        is_bot=is_bot,
        full_name=name,
        username=None,
        mention_html=lambda: f'<a href="tg://user?id={user_id}">{name}</a>',
    )


BOT_ID = 999
SPAMMER = user(7, "加V领福利")


def admin(user_id, *, is_bot=False, delete=True, restrict=True):
    return SimpleNamespace(
        user=user(user_id, is_bot=is_bot),
        status="administrator",
        can_delete_messages=delete,
        can_restrict_members=restrict,
    )


class FakeBot:
    id = BOT_ID

    def __init__(self, *, members=150, rights=True, dm_errors=(), delete_error=None):
        self.admins = [admin(1), admin(2), admin(BOT_ID, is_bot=True, delete=rights, restrict=rights)]
        self.member_count = members
        self.dm_errors = set(dm_errors)
        self.delete_error = delete_error
        self.group_posts = []
        self.dms = []
        self.deleted = []
        self.bans = []
        self.unbans = []
        self.chat_members = {}

    async def get_chat_administrators(self, chat_id):
        return self.admins

    async def get_chat_member_count(self, chat_id):
        return self.member_count

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None, link_preview_options=None):
        if chat_id < 0:
            self.group_posts.append(text)
            return
        if chat_id in self.dm_errors:
            raise Forbidden("bot can't initiate conversation with a user")
        self.dms.append(SimpleNamespace(chat_id=chat_id, text=text, markup=reply_markup))

    async def delete_message(self, chat_id, message_id):
        if self.delete_error:
            raise self.delete_error
        self.deleted.append(message_id)

    async def ban_chat_member(self, chat_id, user_id, until_date=None):
        self.bans.append((chat_id, user_id, until_date))

    async def unban_chat_member(self, chat_id, user_id, only_if_banned=False):
        self.unbans.append((chat_id, user_id, only_if_banned))

    async def get_chat_member(self, chat_id, user_id):
        return self.chat_members.get(user_id) or SimpleNamespace(status="member", user=user(user_id))


CHAT = SimpleNamespace(id=CHAT_ID, type="supergroup", title="FogMoe 交流群", username=None)


def target(*, sender=SPAMMER, sender_chat=None, hours_ago=1, message_id=55):
    return SimpleNamespace(
        chat=CHAT,
        chat_id=CHAT_ID,
        message_id=message_id,
        from_user=sender,
        sender_chat=sender_chat,
        date=datetime.now(UTC) - timedelta(hours=hours_ago),
        text="大家好，看我名字",
        caption=None,
        entities=(),
        caption_entities=(),
        forward_origin=None,
        reply_to_message=None,
        is_automatic_forward=False,
    )


def run_report(bot, reporter_id, reported=None, *, sender_chat=None):
    replies = []

    async def reply_text(text, **kwargs):
        replies.append(text)

    message = SimpleNamespace(
        reply_to_message=reported if reported is not None else target(),
        from_user=user(reporter_id, f"成员{reporter_id}"),
        sender_chat=sender_chat,
        reply_text=reply_text,
    )
    update = SimpleNamespace(message=message, effective_chat=CHAT, effective_user=message.from_user)
    command_cooldown.command_cooldowns.clear()  # 同一人连发的举报不受 1 秒冷却影响
    asyncio.run(report.report_command(update, SimpleNamespace(bot=bot)))
    return replies


@pytest.fixture
def jev(monkeypatch):
    """Jev 可用；`jev.set(probability)` 设定下一次的结果，`jev.calls` 是调用记录。"""
    monkeypatch.setattr(judge, "is_configured", lambda: True)
    monkeypatch.setattr(spam_ai_operations, "is_checking", Recorder(result=False))
    monkeypatch.setattr(spam_strikes.spam_ai_operations, "forget_member", Recorder())
    state = SimpleNamespace(calls=[])

    def set_result(probability=None, error=None):
        judgment = None if probability is None else judge.Judgment(probability, "jev-1.13.0", 300)
        state.recorder = Recorder(result=judgment, errors=[error])
        state.calls = state.recorder.calls
        monkeypatch.setattr(judge, "judge", state.recorder)

    state.set = set_result
    set_result(0.95)
    return state


@pytest.mark.parametrize(
    ("reported", "reporter_id", "reply"),
    [
        (target(sender=user(5)), 5, report_views.SELF),
        (target(sender=user(8, is_bot=True)), 5, report_views.BOT),
        (target(sender=user(1)), 5, report_views.ADMIN),
        (target(sender_chat=SimpleNamespace(id=CHAT_ID, title="FogMoe 交流群")), 5, report_views.ADMIN),
        (target(hours_ago=30), 5, report_views.TOO_OLD),
    ],
    ids=["self", "bot", "admin", "anonymous-admin", "too-old"],
)
def test_reports_that_cannot_be_acted_on_are_refused(jev, reported, reporter_id, reply):
    bot = FakeBot()

    assert run_report(bot, reporter_id, reported) == [reply]
    assert bot.dms == [] and jev.calls == []


@pytest.mark.parametrize(
    ("account", "sender_chat"),
    [(1087968824, SimpleNamespace(id=CHAT_ID)), (136817688, SimpleNamespace(id=-100999))],
    ids=["anonymous-admin", "as-channel"],
)
def test_reports_sent_anonymously_or_as_a_channel_are_refused(jev, account, sender_chat):
    bot = FakeBot(members=40)

    assert run_report(bot, account, sender_chat=sender_chat) == [report_views.AS_CHAT]
    assert bot.dms == [] and jev.calls == []


def test_the_first_report_notifies_admins_once_and_waits_for_more_votes(jev):
    bot = FakeBot(members=150)

    first = run_report(bot, 11)
    second_try = run_report(bot, 11)

    assert first == [
        "已收到举报（1/2）。已通知管理员。凑够 2 人举报后由 AI 判断，确认是垃圾信息会自动删除并封禁。"
    ]
    assert second_try == [report_views.DUPLICATE]
    assert [dm.chat_id for dm in bot.dms] == [1, 2]  # 机器人自己不收私聊
    assert "新举报" in bot.dms[0].text and "凑够 2 人举报" in bot.dms[0].text
    buttons = bot.dms[0].markup.inline_keyboard[0]
    assert [button.text for button in buttons] == ["查看消息", "删除并封禁 24 小时"]
    assert buttons[1].callback_data == report_views.ban_data(CHAT_ID, 55, SPAMMER.id)
    assert jev.calls == []


def test_enough_votes_and_a_spam_verdict_delete_kick_and_ban_for_a_day(jev):
    bot = FakeBot(members=150)
    run_report(bot, 11)
    bot.dms.clear()

    replies = run_report(bot, 12)

    assert replies == []  # 群里的移出公告就是回复
    review = jev.calls[0][0][0]
    assert (review.text, review.sender_name) == ("大家好，看我名字", "加V领福利")
    assert bot.deleted == [55]
    (chat_id, user_id, until), = bot.bans
    assert (chat_id, user_id) == (CHAT_ID, SPAMMER.id)
    assert timedelta(hours=23) < until - datetime.now(UTC) <= timedelta(hours=24)
    assert bot.group_posts == [
        '🚫 <a href="tg://user?id=7">加V领福利</a> 的消息经 2 人举报、AI 确认为垃圾信息，'
        "已被移出群组，24 小时内无法重新加入。"
    ]
    assert [dm.chat_id for dm in bot.dms] == [1, 2]
    assert "举报已自动处理" in bot.dms[0].text and "95%" in bot.dms[0].text
    (button,), = bot.dms[0].markup.inline_keyboard
    action = report_views.parse_action(button.callback_data)
    assert (button.text, action.kind, action.chat_id, action.user_id) == ("解除封禁", "unban", CHAT_ID, SPAMMER.id)
    assert 23 * 3600 < action.until - datetime.now(UTC).timestamp() <= 24 * 3600
    # 处理过之后的举报只计票
    assert run_report(bot, 13) == ["已收到举报（3 人），这条消息已经处理过了。"]
    assert len(jev.calls) == 1


def test_a_message_that_could_not_be_deleted_is_flagged_to_admins(jev):
    bot = FakeBot(members=40, delete_error=BadRequest("Message can't be deleted"))

    run_report(bot, 11)

    assert bot.bans
    assert "已封禁 24 小时，但消息没能删除，请手动删除" in bot.dms[0].text
    assert [button.text for button in bot.dms[0].markup.inline_keyboard[0]] == ["查看消息", "解除封禁"]


def test_a_message_already_gone_counts_as_deleted(jev):
    bot = FakeBot(members=40, delete_error=BadRequest("Message to delete not found"))

    run_report(bot, 11)

    assert "举报已自动处理" in bot.dms[0].text


def test_a_small_group_acts_on_the_first_report_with_a_single_admin_message(jev):
    bot = FakeBot(members=40)

    run_report(bot, 11)

    assert bot.bans and [dm.chat_id for dm in bot.dms] == [1, 2]
    assert all("举报已自动处理" in dm.text for dm in bot.dms)


def test_a_clean_verdict_hands_the_report_to_admins(jev):
    jev.set(0.2)
    bot = FakeBot(members=40)

    replies = run_report(bot, 11)

    assert replies == ["已收到举报（1/1）。AI 认为这条消息不是垃圾信息，已交给管理员处理。"]
    assert bot.bans == [] and bot.deleted == []
    assert "AI 认为这条消息不是垃圾信息（概率 20%）" in bot.dms[0].text
    assert bot.dms[0].markup.inline_keyboard[0][1].text == "删除并封禁 24 小时"


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        (lambda monkeypatch, bot: monkeypatch.setattr(judge, "is_configured", lambda: False), "AI 识别暂未开放"),
        (lambda monkeypatch, bot: setattr(bot.admins[2], "can_restrict_members", False), "机器人没有删除消息或封禁成员的权限"),
        (lambda monkeypatch, bot: monkeypatch.setattr(votes, "FREE_GROUP_DAILY_CHECKS", 0), "本群今天由举报触发的 AI 判断次数已用完"),
        (lambda monkeypatch, bot: monkeypatch.setattr(votes, "FREE_TOTAL_DAILY_CHECKS", 0), "今天的 AI 判断次数已用完"),
    ],
    ids=["ai-off", "no-rights", "group-limit", "total-limit"],
)
def test_reports_that_cannot_reach_ai_go_to_admins(jev, monkeypatch, setup, reason):
    bot = FakeBot(members=40)
    setup(monkeypatch, bot)

    replies = run_report(bot, 11)

    assert replies == [f"已收到举报（1/1）。{reason}，已交给管理员处理。"]
    assert jev.calls == [] and bot.bans == []
    assert f"但{reason}" in bot.dms[0].text
    # 转交之后不再尝试
    assert run_report(bot, 12) == ["已收到举报（2 人），这条消息已经处理过了。"]


def test_a_failed_judgment_goes_to_admins(jev):
    jev.set(error=judge.JudgeError("HTTP 429"))
    bot = FakeBot(members=40)

    assert run_report(bot, 11) == ["已收到举报（1/1）。AI 判断暂时失败，已交给管理员处理。"]
    assert bot.bans == []


def test_a_reporter_out_of_quota_leaves_the_judgment_to_the_next_reporter(jev, monkeypatch):
    monkeypatch.setattr(votes, "REPORTER_DAILY_CHECKS", 0)
    bot = FakeBot(members=40)

    replies = run_report(bot, 11)

    assert replies == [
        "已收到举报（1/1）。已通知管理员。你今天触发 AI 判断的次数已用完，需要其他成员举报或管理员处理。"
    ]
    assert "新举报" in bot.dms[0].text and jev.calls == []

    monkeypatch.setattr(votes, "REPORTER_DAILY_CHECKS", 10)
    run_report(bot, 12)
    assert len(jev.calls) == 1 and bot.bans


def test_admins_who_never_messaged_the_bot_are_skipped(jev):
    bot = FakeBot(members=150, dm_errors={1, 2})

    replies = run_report(bot, 11)

    assert replies == [f"已收到举报（1/2）。{report_views.NO_ADMIN_REACHED}凑够 2 人举报后由 AI 判断，确认是垃圾信息会自动删除并封禁。"]


def test_messages_sent_as_a_channel_only_notify_admins(jev):
    bot = FakeBot(members=40)
    reported = target(sender=user(136817688, "Channel", is_bot=True), sender_chat=SimpleNamespace(id=-100999, title="福利频道"))

    first = run_report(bot, 11, reported)
    second = run_report(bot, 12, reported)

    assert first == ["已收到举报。已通知管理员。这条消息是以频道身份发送的，不会自动处理。"]
    assert second == ["已收到举报。这条消息是以频道身份发送的，不会自动处理，需要管理员处理。"]
    assert jev.calls == [] and bot.bans == []
    assert len(bot.dms) == 2 and "以频道身份发送" in bot.dms[0].text
    assert [button.text for button in bot.dms[0].markup.inline_keyboard[0]] == ["查看消息"]


# ---------------------------------------------------------------------------
# 管理员私聊里的按钮
# ---------------------------------------------------------------------------


def notice_markup(*datas):
    return InlineKeyboardMarkup([[InlineKeyboardButton(data, callback_data=data) for data in datas]])


def press(bot, data, clicker_id=1, markup=None):
    """点私聊里的按钮；默认这个按钮就在那条私聊上。"""
    query = SimpleNamespace(
        data=data,
        from_user=user(clicker_id, "管理员"),
        message=SimpleNamespace(text_html="📣 <b>新举报</b>", reply_markup=markup or notice_markup(data)),
        answer=Recorder(),
        edit_message_text=Recorder(),
        edit_message_reply_markup=Recorder(),
    )
    asyncio.run(report.report_admin_callback(SimpleNamespace(callback_query=query), SimpleNamespace(bot=bot)))
    return query


def unban_button(hours_left=12):
    until = int(datetime.now(UTC).timestamp() + hours_left * 3600)
    return report_views.unban_data(CHAT_ID, SPAMMER.id, until)


def test_buttons_that_are_not_on_the_notice_are_ignored(jev):
    bot = FakeBot()
    bot.chat_members[1] = admin(1)
    forged = report_views.ban_data(CHAT_ID, 77, 42)

    query = press(bot, forged, markup=notice_markup(report_views.ban_data(CHAT_ID, 55, SPAMMER.id)))

    assert query.answer.calls == [((report_views.STALE_BUTTON,), {"show_alert": True})]
    assert bot.bans == [] and bot.deleted == []


def test_an_unban_button_stops_working_once_the_ban_has_expired(jev):
    bot = FakeBot()
    bot.chat_members[1] = admin(1)

    data = unban_button(hours_left=-1)
    link = InlineKeyboardButton("查看消息", url="https://t.me/c/123/55")

    query = press(bot, data, markup=InlineKeyboardMarkup([[link, InlineKeyboardButton("解除封禁", callback_data=data)]]))

    assert query.answer.calls == [((report_views.UNBAN_EXPIRED,), {"show_alert": True})]
    (), kwargs = query.edit_message_reply_markup.calls[0]
    assert kwargs["reply_markup"].inline_keyboard == ((link,),)  # 消息还没删时链接留着
    assert bot.unbans == []
    assert report_views.without_button(notice_markup(data), data) is None


def test_only_admins_who_can_ban_may_press_the_buttons(jev):
    bot = FakeBot()
    bot.chat_members[1] = admin(1, restrict=False)

    query = press(bot, report_views.ban_data(CHAT_ID, 55, SPAMMER.id))

    assert query.answer.calls == [(("只有能封禁成员的管理员可以操作。",), {"show_alert": True})]
    assert bot.bans == [] and query.edit_message_text.calls == []


def test_an_admin_can_ban_from_the_notice_and_then_undo_it(jev):
    bot = FakeBot()
    bot.chat_members[1] = SimpleNamespace(status="creator", user=user(1, "管理员"))
    bot.chat_members[SPAMMER.id] = SimpleNamespace(status="member", user=SPAMMER)

    banned = press(bot, report_views.ban_data(CHAT_ID, 55, SPAMMER.id))

    assert bot.deleted == [55] and bot.bans[0][:2] == (CHAT_ID, SPAMMER.id)
    assert "的消息被管理员确认为垃圾信息" in bot.group_posts[0]
    (text,), kwargs = banned.edit_message_text.calls[0]
    assert text.startswith("📣 <b>新举报</b>\n\n✅ ") and "已删除消息并封禁 24 小时" in text
    unban = kwargs["reply_markup"].inline_keyboard[0][0]
    assert unban.text == "解除封禁"

    unbanned = press(bot, unban.callback_data)

    assert bot.unbans == [(CHAT_ID, SPAMMER.id, True)]
    (text,), kwargs = unbanned.edit_message_text.calls[0]
    assert "已解除封禁" in text and kwargs["reply_markup"] is None


def test_a_failed_unban_keeps_the_buttons(jev, monkeypatch):
    bot = FakeBot()
    bot.chat_members[1] = admin(1)
    monkeypatch.setattr(bot, "unban_chat_member", Recorder(errors=[BadRequest("Chat not found")]))

    data = unban_button()
    markup = notice_markup(data)
    query = press(bot, data, markup=markup)

    (text,), kwargs = query.edit_message_text.calls[0]
    assert report_views.ADMIN_UNBAN_FAILED in text and kwargs["reply_markup"] is markup


def test_a_ban_from_the_notice_that_could_not_delete_says_so(jev):
    bot = FakeBot(delete_error=BadRequest("Message can't be deleted"))
    bot.chat_members[1] = admin(1)

    query = press(bot, report_views.ban_data(CHAT_ID, 55, SPAMMER.id))

    (text,), _ = query.edit_message_text.calls[0]
    assert bot.bans and "已封禁 24 小时，但消息没能删除，请手动删除" in text
