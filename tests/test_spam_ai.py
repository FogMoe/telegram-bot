"""/spam ai 与垃圾信息累计处罚：请求组装与解析、付费与检查规则、处罚计数、适配层回复（不连网络、不连数据库）。"""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity
from telegram.error import BadRequest
from telegram.ext import ApplicationHandlerStop

from fogmoe_telegram_bot.core import balance
from fogmoe_telegram_bot.features.moderation import (
    admin_ban_views,
    spam_control,
    spam_control_views,
    spam_strikes,
)
from fogmoe_telegram_bot.features.moderation.operations import deleted_messages, join_messages
from fogmoe_telegram_bot.features.moderation.operations import spam_control as spam_rules
from fogmoe_telegram_bot.features.moderation.repositories.spam_settings import SpamSettings
from fogmoe_telegram_bot.features.moderation.spam_ai import handlers, judge, operations
from fogmoe_telegram_bot.features.moderation.spam_ai.operations import PayResult, PayStatus
from fogmoe_telegram_bot.features.moderation.spam_ai.repositories import groups
from fogmoe_telegram_bot.features.moderation.spam_ai.repositories.groups import (
    DueReminder,
    ReminderKind,
    SpamAiGroup,
)


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


NOW = datetime(2026, 10, 7, 12, 0)


@pytest.fixture(autouse=True)
def _reset_memory_state():
    operations._exempt.clear()
    operations._daily_checks.clear()
    operations._status_cache.clear()
    spam_strikes.TRACKER._hits.clear()
    deleted_messages.ORIGINALS._items.clear()
    yield
    operations._exempt.clear()
    operations._daily_checks.clear()
    operations._status_cache.clear()
    spam_strikes.TRACKER._hits.clear()
    deleted_messages.ORIGINALS._items.clear()


def group(*, enabled=True, days_left=10.0, chat_id=-100):
    paid_until = NOW + timedelta(days=days_left)
    return SpamAiGroup(chat_id, enabled, paid_until, active=days_left > 0)


# ---------------------------------------------------------------------------
# 请求与解析
# ---------------------------------------------------------------------------


def review(**overrides):
    values = dict(text="招聘手机兼职，日结300+，有意私聊", is_caption=False, sender_name="日结兼职 小美")
    values.update(overrides)
    return judge.MessageForReview(**values)


def test_the_request_carries_the_message_sender_and_one_noul_question(settings_override):
    settings_override(TYPESAFE_MODEL="jev-1.13.0")
    request = judge.build_request(
        review(
            sender_username="xm8888",
            group_title="FogMoe 交流群",
            forwarded_from="福利频道",
            hidden_links=("https://spam.example",),
            reply_to_text="有人吗",
            via_bot="adbot",
            buttons=("加入福利群",),
        )
    )

    assert request["model"] == "jev-1.13.0"
    assert request["state"] == {
        "group_title": "FogMoe 交流群",
        "sender": {"name": "日结兼职 小美", "username": "xm8888"},
        "message": {
            "text": "招聘手机兼职，日结300+，有意私聊",
            "is_caption": False,
            "forwarded_from": "福利频道",
            "hidden_links": ["https://spam.example"],
            "reply_to_text": "有人吗",
            "via_bot": "adbot",
            "buttons": ["加入福利群"],
        },
    }
    question = request["questions"]["spam"]
    assert question["type"] == "noul"
    assert question["criteria"]["true"] and question["criteria"]["false"]


def test_long_texts_are_truncated():
    state = judge.build_state(review(text="x" * 5000, reply_to_text="y" * 500))

    assert len(state["message"]["text"]) == judge.MAX_TEXT_CHARS + 1
    assert len(state["message"]["reply_to_text"]) == judge.MAX_REPLY_CHARS + 1


def test_buttons_and_hidden_links_are_capped():
    state = judge.build_state(review(buttons=("z" * 100,) * 30, hidden_links=("https://a.example",) * 30))

    assert len(state["message"]["buttons"]) == len(state["message"]["hidden_links"]) == judge.MAX_LIST_ITEMS
    assert len(state["message"]["buttons"][0]) == judge.MAX_BUTTON_CHARS + 1


def test_parse_judgment_reads_the_probability_model_and_tokens():
    payload = {
        "model": "jev-1.13.0",
        "answers": {"spam": {"type": "noul", "noul": 0.95}},
        "usage": {"input_tokens": 412, "output_tokens": 20},
    }

    assert judge.parse_judgment(payload) == judge.Judgment(0.95, "jev-1.13.0", 412)


@pytest.mark.parametrize(
    "payload",
    [{}, {"answers": {}}, {"answers": {"spam": {"noul": "high"}}}, {"answers": {"spam": {"noul": 1.5}}}, []],
)
def test_parse_judgment_rejects_unexpected_payloads(payload):
    with pytest.raises(judge.JudgeError):
        judge.parse_judgment(payload)


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def json(self, content_type=None):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.posts = []

    def post(self, url, json):
        self.posts.append((url, json))
        return self.response


def test_request_judgment_posts_to_the_configured_api(settings_override):
    settings_override(TYPESAFE_API_BASE="https://ts.example/")
    session = FakeSession(FakeResponse(200, {"answers": {"spam": {"noul": 0.1}}}))

    judgment = asyncio.run(judge.request_judgment(session, review()))

    assert judgment.spam_probability == 0.1
    assert session.posts[0][0] == "https://ts.example/v1/systemone"


@pytest.mark.parametrize("status", [401, 422, 429, 529])
def test_request_judgment_fails_on_http_errors(status):
    session = FakeSession(FakeResponse(status, {"detail": "nope"}))

    with pytest.raises(judge.JudgeError, match=str(status)):
        asyncio.run(judge.request_judgment(session, review()))


@pytest.mark.parametrize(("key", "configured"), [(None, False), ("", False), ("  ", False), ("ts-key", True)])
def test_is_configured_needs_a_key(settings_override, key, configured):
    settings_override(TYPESAFE_API_KEY=key)

    assert judge.is_configured() is configured


# ---------------------------------------------------------------------------
# 检查规则
# ---------------------------------------------------------------------------


def test_the_daily_limit_stops_checks_until_the_next_utc_day(monkeypatch):
    monkeypatch.setattr(operations, "DAILY_CHECK_LIMIT", 2)
    today, tomorrow = date(2026, 10, 7), date(2026, 10, 8)

    assert operations.take_daily_check(-100, today)
    assert operations.take_daily_check(-100, today)
    assert not operations.take_daily_check(-100, today)
    assert operations.daily_limit_reached(-100, today)
    assert operations.take_daily_check(-200, today)
    assert operations.take_daily_check(-100, tomorrow)
    assert not operations.daily_limit_reached(-100, tomorrow)


@pytest.mark.parametrize(("probability", "spam"), [(0.8, True), (0.99, True), (0.79, False), (0.0, False)])
def test_messages_at_or_above_the_threshold_are_spam(probability, spam):
    assert operations.is_spam(probability) is spam


def test_members_with_enough_clean_messages_are_not_checked_again(monkeypatch):
    counts = {7: 4, 8: 5}

    async def checked_count(chat_id, user_id):
        return counts[user_id]

    monkeypatch.setattr(groups, "get_checked_count", checked_count)

    assert asyncio.run(operations.needs_review(-100, 7))
    assert not asyncio.run(operations.needs_review(-100, 8))
    assert operations.is_exempt(-100, 8) and not operations.is_exempt(-100, 7)


@asynccontextmanager
async def fake_transaction():
    yield "conn"


def test_the_fifth_clean_message_exempts_the_member(monkeypatch):
    monkeypatch.setattr(operations.sql, "transaction", fake_transaction)
    monkeypatch.setattr(groups, "add_checked", Recorder(result=5))

    assert asyncio.run(operations.record_clean(-100, 7)) == 5
    assert operations.is_exempt(-100, 7)


def test_a_kicked_member_is_checked_again_after_rejoining(monkeypatch):
    monkeypatch.setattr(operations.sql, "transaction", fake_transaction)
    forget = Recorder()
    monkeypatch.setattr(groups, "forget_member", forget)
    operations._mark_exempt(-100, 7)

    asyncio.run(operations.forget_member(-100, 7))

    assert not operations.is_exempt(-100, 7)
    assert forget.calls == [(("conn", -100, 7), {})]


@pytest.mark.parametrize(
    ("state", "checking"),
    [(None, False), (group(), True), (group(enabled=False), False), (group(days_left=-1), False)],
)
def test_only_paid_running_groups_are_checked(monkeypatch, state, checking):
    monkeypatch.setattr(operations, "utcnow", lambda: NOW)
    monkeypatch.setattr(groups, "get_group", Recorder(result=state))

    assert asyncio.run(operations.is_checking(-100)) is checking


def test_the_group_state_is_cached(monkeypatch):
    monkeypatch.setattr(operations, "utcnow", lambda: NOW)
    get_group = Recorder(result=group())
    monkeypatch.setattr(groups, "get_group", get_group)

    asyncio.run(operations.is_checking(-100))
    asyncio.run(operations.is_checking(-100))
    operations.forget_status(-100)
    asyncio.run(operations.is_checking(-100))

    assert len(get_group.calls) == 2


# ---------------------------------------------------------------------------
# 付费
# ---------------------------------------------------------------------------


class Billing:
    """打桩一次付费事务：群状态前后各一份，记录扣款与延期。"""

    def __init__(self, monkeypatch, before, *, total=500, applied=True, registered=True):
        self.after = SpamAiGroup(-100, True, NOW + timedelta(days=40), True)
        self.states = [before, self.after]
        self.debits = []
        self.extended = []
        self.resumed = []

        async def run_in_transaction(work):
            return await work("conn")

        async def get_group(chat_id, *, connection=None, for_update=False):
            return self.states.pop(0)

        async def debit(connection, user_id, amount, *, op_key, reason, ref=None):
            if not registered:
                raise balance.UserNotFound(user_id)
            if total < amount:
                raise balance.InsufficientBalance(user_id, amount, balance.UserBalances(total, 0))
            self.debits.append((user_id, amount, op_key, reason, ref))
            return SimpleNamespace(applied=applied)

        async def extend_period(connection, chat_id, *, user_id, days):
            self.extended.append((chat_id, user_id, days))

        async def resume(connection, chat_id):
            self.resumed.append(chat_id)
            return True

        monkeypatch.setattr(balance, "run_in_transaction", run_in_transaction)
        monkeypatch.setattr(balance, "debit", debit)
        monkeypatch.setattr(groups, "get_group", get_group)
        monkeypatch.setattr(groups, "extend_period", extend_period)
        monkeypatch.setattr(groups, "resume", resume)


def test_enabling_a_new_group_charges_one_period(monkeypatch):
    billing = Billing(monkeypatch, before=None)

    result = asyncio.run(operations.enable(-100, 7, "spamai:-100:11"))

    assert result == PayResult(PayStatus.CHARGED, paid_until=billing.after.paid_until)
    assert billing.debits == [(7, 100, "spamai:-100:11", "spam_ai", "chat:-100")]
    assert billing.extended == [(-100, 7, 30)]


def test_enabling_an_expired_group_charges_again(monkeypatch):
    billing = Billing(monkeypatch, before=group(days_left=-3))

    result = asyncio.run(operations.enable(-100, 7, "k"))

    assert result.status is PayStatus.CHARGED and not result.extended
    assert len(billing.debits) == 1


def test_enabling_within_the_paid_period_does_not_charge(monkeypatch):
    paused = group(enabled=False)
    billing = Billing(monkeypatch, before=paused)

    result = asyncio.run(operations.enable(-100, 7, "k"))

    assert result == PayResult(PayStatus.RESUMED, paid_until=paused.paid_until)
    assert billing.debits == [] and billing.resumed == [-100]


def test_enabling_a_running_group_changes_nothing(monkeypatch):
    billing = Billing(monkeypatch, before=group())

    assert asyncio.run(operations.enable(-100, 7, "k")).status is PayStatus.ALREADY_ON
    assert billing.debits == [] and billing.resumed == []


def test_renewing_a_paused_group_extends_and_resumes(monkeypatch):
    billing = Billing(monkeypatch, before=group(enabled=False))

    result = asyncio.run(operations.renew(-100, 7, "k"))

    assert (result.status, result.extended, result.resumed) == (PayStatus.CHARGED, True, True)
    assert billing.extended == [(-100, 7, 30)]


def test_a_replayed_payment_does_not_extend_twice(monkeypatch):
    billing = Billing(monkeypatch, before=group(), applied=False)

    assert asyncio.run(operations.renew(-100, 7, "k")).status is PayStatus.CHARGED
    assert billing.extended == []


def test_payment_needs_enough_coins_and_a_registered_user(monkeypatch):
    billing = Billing(monkeypatch, before=None, total=99)
    result = asyncio.run(operations.renew(-100, 7, "k"))
    assert result == PayResult(PayStatus.INSUFFICIENT, balance_total=99)
    assert billing.debits == [] and billing.extended == []

    billing = Billing(monkeypatch, before=None, registered=False)
    assert asyncio.run(operations.renew(-100, 7, "k")).status is PayStatus.NOT_REGISTERED
    assert billing.extended == []


def test_moving_a_migrated_group_forgets_both_cached_states(monkeypatch):
    monkeypatch.setattr(operations.sql, "transaction", fake_transaction)
    move = Recorder(result=True)
    monkeypatch.setattr(groups, "move_chat", move)
    operations._status_cache[-1] = (0.0, None)
    operations._status_cache[-1001] = (0.0, None)

    assert asyncio.run(operations.move_group(-1, -1001))
    assert move.calls == [(("conn", -1, -1001), {})]
    assert operations._status_cache == {}


def test_payment_op_key_comes_from_the_command_message():
    assert operations.payment_op_key(-100, 11) == "spamai:-100:11"


# ---------------------------------------------------------------------------
# 累计处罚
# ---------------------------------------------------------------------------


def test_strikes_count_within_a_sliding_window():
    now = [0.0]
    tracker = spam_strikes.StrikeTracker(3600, clock=lambda: now[0])

    assert tracker.record(-100, 7) == 1
    now[0] = 3000
    assert tracker.record(-100, 7) == 2
    now[0] = 3700  # 第一次已经出了窗口
    assert tracker.record(-100, 7) == 2
    assert tracker.record(-100, 8) == 1
    tracker.clear(-100, 7)
    assert tracker.record(-100, 7) == 1


class FakeBot:
    def __init__(self, ban_error=None):
        self.sent = []
        self.markups = []
        self.bans = []
        self.deleted = []
        self._ban_error = ban_error

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.sent.append(text)
        self.markups.append(reply_markup)

    async def ban_chat_member(self, chat_id, user_id, until_date=None):
        if self._ban_error:
            raise self._ban_error
        self.bans.append((chat_id, user_id, until_date))

    async def delete_message(self, chat_id, message_id):
        self.deleted.append(message_id)


def message(*, user_id=7, is_bot=False, sender_chat=None, chat_type="supergroup", **fields):
    user = SimpleNamespace(
        id=user_id,
        is_bot=is_bot,
        full_name="小美",
        username="xm",
        mention_html=lambda: f'<a href="tg://user?id={user_id}">小美</a>',
    )
    values = dict(
        chat_id=-100,
        message_id=55,
        chat=SimpleNamespace(type=chat_type, title="FogMoe 交流群"),
        from_user=user,
        sender_chat=sender_chat,
        is_automatic_forward=False,
        text="hi",
        caption=None,
        entities=(),
        caption_entities=(),
        forward_origin=None,
        reply_to_message=None,
        reply_markup=None,
        via_bot=None,
        guest_bot_caller_user=None,
        guest_bot_caller_chat=None,
    )
    values.update(fields)
    return SimpleNamespace(**values)


def buttons(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row]


def penalize(bot, msg):
    asyncio.run(
        spam_strikes.penalize(
            bot, msg, original="加我领福利", reason_html="的消息被 AI 识别为垃圾信息", note="如果是误判，请联系管理员。"
        )
    )


def test_the_third_strike_kicks_and_bans_for_a_day(monkeypatch):
    forget = Recorder()
    monkeypatch.setattr(spam_strikes.spam_ai_operations, "forget_member", forget)
    monkeypatch.setattr(join_messages, "JOINS", join_messages.JoinMessages())
    join_messages.JOINS.record(-100, 7, 30)
    bot = FakeBot()

    for _ in range(3):
        penalize(bot, message())

    assert "这是第 1 次警告，1 小时内满 3 次将被移出群组。如果是误判" in bot.sent[0]
    assert "这是第 2 次警告" in bot.sent[1]
    assert bot.sent[2].startswith("🚫") and "24 小时内无法重新加入" in bot.sent[2]
    # 警告和公告都不显示名字；警告下面带管理员看原文和封禁的按钮，移出公告下面只有看原文
    assert all("小美" not in text and "tg://user?id=7" in text for text in bot.sent)
    assert buttons(bot.markups[0]) == [admin_ban_views.view_data(-100, 55), admin_ban_views.ban_data(-100, 7)]
    assert buttons(bot.markups[2]) == [admin_ban_views.view_data(-100, 55)]
    assert deleted_messages.ORIGINALS.get(-100, 55) == "加我领福利"
    (chat_id, user_id, until), = bot.bans
    assert (chat_id, user_id) == (-100, 7)
    assert timedelta(hours=23) < until - datetime.now(UTC) <= timedelta(hours=24)
    assert forget.calls == [((-100, 7), {})]
    assert bot.deleted == [30]  # 加群消息也删了
    # 移出后重新计数
    penalize(bot, message())
    assert "这是第 1 次警告" in bot.sent[3]


def test_basic_groups_do_not_promise_a_rejoin_cooldown(monkeypatch):
    monkeypatch.setattr(spam_strikes.spam_ai_operations, "forget_member", Recorder())
    bot = FakeBot()

    for _ in range(3):
        penalize(bot, message(chat_type="group"))

    assert bot.sent[2].endswith("已被移出群组。")


def test_a_permanent_kick_says_so(monkeypatch):
    monkeypatch.setattr(spam_strikes.spam_ai_operations, "forget_member", Recorder())
    bot = FakeBot()

    assert asyncio.run(spam_strikes.kick(bot, -100, 7, "该用户", offense="该用户 的消息被管理员确认为垃圾信息", seconds=None))

    assert bot.bans == [(-100, 7, None)]
    assert bot.sent == ["🚫 该用户 的消息被管理员确认为垃圾信息，已被移出群组并永久封禁。"]


@pytest.mark.parametrize(
    ("error", "reply"),
    [
        (BadRequest("Not enough rights to restrict/ban chat member"), "没有封禁成员的权限"),
        (BadRequest("User not found"), "移出失败，请管理员手动处理"),
    ],
)
def test_a_failed_kick_is_reported(monkeypatch, error, reply):
    forget = Recorder()
    monkeypatch.setattr(spam_strikes.spam_ai_operations, "forget_member", forget)
    bot = FakeBot(ban_error=error)

    for _ in range(3):
        penalize(bot, message())

    assert reply in bot.sent[2]
    assert forget.calls == []


@pytest.mark.parametrize(
    "msg",
    [message(sender_chat=SimpleNamespace(title="某频道")), message(is_bot=True)],
    ids=["as-channel", "bot"],
)
def test_messages_without_a_person_behind_them_are_never_kicked(msg):
    bot = FakeBot()

    for _ in range(4):
        penalize(bot, msg)

    assert bot.bans == []
    assert all("次警告" not in text for text in bot.sent)
    # 没有可以封禁的个人，只能看原文
    assert all(buttons(markup) == [admin_ban_views.view_data(-100, 55)] for markup in bot.markups)


# ---------------------------------------------------------------------------
# 适配层：检查一条消息
# ---------------------------------------------------------------------------


def test_review_input_reads_captions_hidden_links_forwards_and_replies():
    msg = message(
        text=None,
        caption="点击领取",
        caption_entities=(
            MessageEntity(MessageEntity.TEXT_LINK, 0, 4, url="https://spam.example"),
            MessageEntity(MessageEntity.BOLD, 0, 2),
        ),
        forward_origin=SimpleNamespace(chat=SimpleNamespace(title="福利频道")),
        reply_to_message=SimpleNamespace(text=None, caption="图片说明"),
    )

    data = handlers.review_input(msg)

    assert (data.text, data.is_caption) == ("点击领取", True)
    assert data.hidden_links == ("https://spam.example",)
    assert data.forwarded_from == "福利频道"
    assert data.reply_to_text == "图片说明"
    assert (data.sender_name, data.sender_username, data.group_title) == ("小美", "xm", "FogMoe 交流群")
    assert (data.via_bot, data.buttons) == (None, ())


def inline_bot_ad(**fields):
    """经 inline bot 发的广告：一张图，没有说明文字，广告在按钮上。"""
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("加入福利群", url="https://t.me/spamgroup")],
            [InlineKeyboardButton("👍", callback_data="like")],
        ]
    )
    values = dict(text=None, caption=None, reply_markup=keyboard, via_bot=SimpleNamespace(username="adbot"))
    values.update(fields)
    return message(**values)


def test_review_input_reads_inline_bot_buttons():
    data = handlers.review_input(inline_bot_ad())

    assert (data.text, data.is_caption) == ("", True)
    assert data.via_bot == "adbot"
    assert data.buttons == ("加入福利群", "👍")
    assert data.hidden_links == ("https://t.me/spamgroup",)


@pytest.mark.parametrize(
    ("msg", "expected"),
    [
        (message(), True),
        (message(is_automatic_forward=True), False),
        (message(sender_chat=SimpleNamespace(title="频道")), False),
        (message(is_bot=True), False),
    ],
)
def test_only_people_speaking_as_themselves_are_reviewed(msg, expected):
    assert handlers.should_review(msg) is expected


def stub_review(monkeypatch, *, probability=None, error=None, needs=True, quota=True):
    monkeypatch.setattr(operations, "needs_review", Recorder(result=needs))
    monkeypatch.setattr(operations, "take_daily_check", lambda chat_id: quota)
    judgment = None if probability is None else judge.Judgment(probability, "jev-1.13.0", 400)
    judge_call = Recorder(result=judgment, errors=[error])
    monkeypatch.setattr(judge, "judge", judge_call)
    clean = Recorder(result=1)
    monkeypatch.setattr(operations, "record_clean", clean)
    punish = Recorder()
    monkeypatch.setattr(spam_strikes, "penalize", punish)
    return SimpleNamespace(judge=judge_call, clean=clean, punish=punish)


def test_spam_is_deleted_and_counted_as_a_strike(monkeypatch):
    calls = stub_review(monkeypatch, probability=0.93)
    bot = FakeBot()

    asyncio.run(handlers.review_message(message(), bot))

    assert bot.deleted == [55]
    (_, msg), kwargs = calls.punish.calls[0]
    assert kwargs == {"original": "hi", "reason_html": "的消息被 AI 识别为垃圾信息", "note": "如果是误判，请联系管理员。"}
    assert calls.clean.calls == []


def test_a_clean_message_counts_toward_the_member_quota(monkeypatch):
    calls = stub_review(monkeypatch, probability=0.1)
    bot = FakeBot()

    asyncio.run(handlers.review_message(message(), bot))

    assert bot.deleted == [] and calls.punish.calls == []
    assert calls.clean.calls == [((-100, 7), {})]


def test_a_failed_judgment_lets_the_message_through_without_counting(monkeypatch):
    calls = stub_review(monkeypatch, error=judge.JudgeError("HTTP 529"))
    bot = FakeBot()

    asyncio.run(handlers.review_message(message(), bot))

    assert bot.deleted == [] and calls.clean.calls == [] and calls.punish.calls == []


@pytest.mark.parametrize(("needs", "quota"), [(False, True), (True, False)])
def test_exempt_members_and_exhausted_quotas_skip_the_model(monkeypatch, needs, quota):
    calls = stub_review(monkeypatch, probability=0.99, needs=needs, quota=quota)

    asyncio.run(handlers.review_message(message(), FakeBot()))

    assert calls.judge.calls == []


# ---------------------------------------------------------------------------
# 适配层：/spam ai
# ---------------------------------------------------------------------------


def test_status_texts():
    def text(state, **kwargs):
        options = dict(now=NOW, limit_reached=False, available=True, filter_enabled=True)
        options.update(kwargs)
        return handlers.status_text(state, **options).split("\n\n")[0]

    assert text(None).startswith("本群尚未开通 AI 识别。开通后，每位成员接下来在本群发的 5 条消息")
    assert text(None, available=False) == "AI 识别暂未开放。"
    assert text(group()) == "AI 识别已开启，有效期至 2026-10-17 12:00 UTC（还剩 10 天）。"
    assert text(group(days_left=0.5)).endswith("（不到 1 天）。")
    assert text(group(), limit_reached=True).endswith("在此之前只使用关键词过滤。")
    assert text(group(), available=False).endswith("AI 识别服务暂时不可用，目前只使用关键词过滤。")
    # 单独开着 AI 识别：总开关关着时照常运行，失效时也没有关键词兜底
    assert text(group(), filter_enabled=False).endswith(
        "垃圾信息过滤总开关没有开启，只用 AI 识别，不检查链接、@提及和垃圾词。"
    )
    assert text(group(), filter_enabled=False, limit_reached=True).endswith("在此之前不检查消息。")
    assert text(group(), filter_enabled=False, available=False).endswith("目前不检查消息。")
    assert text(group(enabled=False)).startswith("AI 识别已暂停，有效期至 2026-10-17 12:00 UTC")
    assert text(group(days_left=-1)) == (
        "本群的 AI 识别已于 2026-10-06 12:00 UTC 到期。/spam ai renew 续费 30 天（100 金币）。"
    )


@pytest.mark.parametrize(
    ("result", "renewing", "reply"),
    [
        (PayResult(PayStatus.CHARGED, NOW), False, "已开启 AI 识别，扣除 100 金币，有效期至 2026-10-07 12:00 UTC。"),
        (PayResult(PayStatus.CHARGED, NOW, extended=True), True, "已续费 30 天，扣除 100 金币，有效期延长至"),
        (PayResult(PayStatus.CHARGED, NOW, extended=True, resumed=True), True, "已续费 30 天并恢复 AI 识别"),
        (PayResult(PayStatus.RESUMED, NOW), False, "已恢复 AI 识别，有效期至 2026-10-07 12:00 UTC，本次没有扣费。"),
        (PayResult(PayStatus.ALREADY_ON, NOW), False, "AI 识别已经是开启状态"),
        (PayResult(PayStatus.INSUFFICIENT, balance_total=40), True, "续费需要 100 金币，你当前只有 40 枚。"),
        (PayResult(PayStatus.NOT_REGISTERED), False, "请先使用 /me 命令注册个人信息，再来开通。"),
    ],
)
def test_pay_texts(result, renewing, reply):
    assert handlers.pay_text(result, renewing=renewing).startswith(reply)


def ai_command(*args, can_delete=True):
    update = SimpleNamespace(
        message=SimpleNamespace(message_id=11, reply_text=Recorder()),
        effective_user=SimpleNamespace(id=7),
        effective_chat=SimpleNamespace(id=-100, type="supergroup"),
    )
    context = SimpleNamespace(
        bot=SimpleNamespace(id=1, get_chat_member=Recorder(result=SimpleNamespace(can_delete_messages=can_delete))),
    )
    return update, context, list(args)


def replies(update):
    return [args[0] for args, _ in update.message.reply_text.calls]


def run_ai(update, context, args, *, filter_enabled=True):
    asyncio.run(handlers.spam_ai_command(update, context, args, filter_enabled=filter_enabled))


@pytest.mark.parametrize("filter_enabled", [True, False])
def test_enabling_charges_with_the_command_message_identity(monkeypatch, settings_override, filter_enabled):
    settings_override(TYPESAFE_API_KEY="ts-key")
    enable = Recorder(result=PayResult(PayStatus.CHARGED, NOW))
    monkeypatch.setattr(operations, "enable", enable)
    update, context, args = ai_command("on")

    # AI 识别可以单独开启，不需要先打开 /spam 总开关
    run_ai(update, context, args, filter_enabled=filter_enabled)

    assert enable.calls == [((-100, 7, "spamai:-100:11"), {})]
    assert replies(update)[0].startswith("已开启 AI 识别")


@pytest.mark.parametrize(
    ("key", "can_delete", "reply"),
    [
        (None, True, "AI 识别暂未开放。"),
        ("ts-key", False, "机器人需要有删除消息的权限才能使用 AI 识别。"),
    ],
)
def test_enabling_checks_preconditions_before_charging(monkeypatch, settings_override, key, can_delete, reply):
    settings_override(TYPESAFE_API_KEY=key)
    renew = Recorder()
    monkeypatch.setattr(operations, "renew", renew)
    update, context, args = ai_command("renew", can_delete=can_delete)

    run_ai(update, context, args)

    assert replies(update) == [reply]
    assert renew.calls == []


def test_an_edited_command_is_not_run_again(monkeypatch, settings_override):
    settings_override(TYPESAFE_API_KEY="ts-key")
    renew = Recorder()
    monkeypatch.setattr(operations, "renew", renew)
    update, context, args = ai_command("renew")
    update.message = None  # 编辑过的命令：PTB 把它放在 edited_message 里

    run_ai(update, context, args)

    assert renew.calls == []


@pytest.mark.parametrize(
    ("fields", "moved"),
    [
        ({"chat_id": -1, "migrate_to_chat_id": -1001, "migrate_from_chat_id": None}, (-1, -1001)),
        ({"chat_id": -1001, "migrate_to_chat_id": None, "migrate_from_chat_id": -1}, (-1, -1001)),
        ({"chat_id": -1, "migrate_to_chat_id": None, "migrate_from_chat_id": None}, None),
    ],
)
def test_a_group_upgrade_moves_the_paid_state(monkeypatch, fields, moved):
    move = Recorder(result=True)
    monkeypatch.setattr(operations, "move_group", move)
    update = SimpleNamespace(effective_message=SimpleNamespace(**fields))

    asyncio.run(handlers.migrate_chat(update, SimpleNamespace()))

    assert move.calls == ([(moved, {})] if moved else [])


def test_pausing_reports_the_unchanged_expiry(monkeypatch):
    monkeypatch.setattr(operations, "pause", Recorder(result=group(enabled=False)))
    update, context, args = ai_command("off")

    run_ai(update, context, args)

    assert replies(update) == [
        "已暂停 AI 识别。有效期仍到 2026-10-17 12:00 UTC，暂停期间照常计算；到期前用 /spam ai on 恢复，不另收费。"
    ]


def test_pausing_a_group_that_is_not_running(monkeypatch):
    monkeypatch.setattr(operations, "pause", Recorder(result=None))
    update, context, args = ai_command("off")

    run_ai(update, context, args)

    assert replies(update) == ["本群当前没有开启 AI 识别。"]


# ---------------------------------------------------------------------------
# 到期提醒
# ---------------------------------------------------------------------------


def test_reminders_are_sent_once_per_claimed_expiry(monkeypatch):
    due = {
        ReminderKind.EXPIRING: [DueReminder(-100, NOW), DueReminder(-200, NOW)],
        ReminderKind.EXPIRED: [DueReminder(-300, NOW)],
    }

    async def due_reminders(kind):
        return due[kind]

    async def claim_reminder(kind, reminder):
        return reminder.chat_id != -200  # 别处已经发过

    monkeypatch.setattr(operations, "due_reminders", due_reminders)
    monkeypatch.setattr(operations, "claim_reminder", claim_reminder)
    send = Recorder()
    context = SimpleNamespace(bot=SimpleNamespace(send_message=send))

    asyncio.run(handlers.remind_expiry_job(context))

    assert [args for args, _ in send.calls] == [
        (-100, "本群的 AI 识别将于 2026-10-07 12:00 UTC 到期。管理员可以用 /spam ai renew 续费 30 天（100 金币）。"),
        (-300, "本群的 AI 识别已到期，不再用 AI 检查消息。管理员可以用 /spam ai renew 续费 30 天（100 金币）。"),
    ]


# ---------------------------------------------------------------------------
# spam_control：关键词与 AI 的衔接
# ---------------------------------------------------------------------------


def group_message_update(msg):
    return SimpleNamespace(
        message=msg,
        edited_message=None,
        effective_message=msg,
        effective_chat=SimpleNamespace(id=-100, type="supergroup"),
    )


def stub_settings(monkeypatch, *, enabled=True, block_links=False, block_mentions=False):
    async def get_settings(chat_id):
        return SpamSettings(enabled=enabled, block_links=block_links, block_mentions=block_mentions)

    monkeypatch.setattr(spam_rules, "get_settings", get_settings)


def stub_filter(monkeypatch, *, keyword_hit, custom_keywords=True, enabled=True):
    async def has_custom(chat_id):
        return custom_keywords

    async def find_spam_word(text, chat_id):
        return "<博彩>" if keyword_hit else None

    stub_settings(monkeypatch, enabled=enabled, block_links=not enabled, block_mentions=not enabled)
    monkeypatch.setattr(spam_rules, "has_custom_keywords", has_custom)
    monkeypatch.setattr(spam_rules, "find_spam_word", find_spam_word)
    punish = Recorder()
    monkeypatch.setattr(spam_strikes, "penalize", punish)
    ai = Recorder()
    monkeypatch.setattr(handlers, "maybe_review", ai)
    return punish, ai


def filter_context(bot):
    return SimpleNamespace(
        bot=bot,
        chat_data={"is_admin:-100:7": False, "is_admin:-100:7_expire": float("inf")},
    )


def test_a_keyword_hit_in_a_caption_is_deleted_and_never_reaches_the_ai(monkeypatch):
    punish, ai = stub_filter(monkeypatch, keyword_hit=True)
    bot = FakeBot()
    msg = message(text=None, caption="来玩博彩")

    asyncio.run(spam_control.process_message(group_message_update(msg), filter_context(bot)))

    assert bot.deleted == [55]
    (_, punished), kwargs = punish.calls[0]
    assert punished is msg
    assert kwargs["reason_html"] == "发送的消息包含垃圾内容 <tg-spoiler>&lt;博彩&gt;</tg-spoiler>"
    assert kwargs["original"] == "来玩博彩"
    assert ai.calls == []


def test_a_global_word_list_hit_only_warns(monkeypatch):
    punish, ai = stub_filter(monkeypatch, keyword_hit=True, custom_keywords=False)
    bot = FakeBot()
    msg = message(text="来玩博彩")

    asyncio.run(spam_control.process_message(group_message_update(msg), filter_context(bot)))

    assert bot.deleted == [55]
    assert punish.calls == [] and ai.calls == []
    assert "&lt;博彩&gt;</tg-spoiler>，已被自动删除。\n这是第" in bot.sent[0]
    assert "移出群组" in bot.sent[0] and bot.bans == []
    assert "小美" not in bot.sent[0] and '<a href="tg://user?id=7">该用户</a>' in bot.sent[0]
    assert buttons(bot.markups[0]) == [admin_ban_views.view_data(-100, 55), admin_ban_views.ban_data(-100, 7)]
    assert deleted_messages.ORIGINALS.get(-100, 55) == "来玩博彩"


def test_messages_the_keywords_miss_go_to_the_ai(monkeypatch):
    punish, ai = stub_filter(monkeypatch, keyword_hit=False)
    bot = FakeBot()
    msg = message(text="大家好")

    asyncio.run(spam_control.process_message(group_message_update(msg), filter_context(bot)))

    assert bot.deleted == [] and punish.calls == []
    assert ai.calls == [((msg, bot), {})]


def test_with_the_filter_off_a_running_ai_check_sees_messages_the_keywords_would_catch(monkeypatch):
    # 总开关关掉时链接与 @提及过滤的设置还留着（stub_filter 里打开了），也不该生效
    punish, ai = stub_filter(monkeypatch, keyword_hit=True, custom_keywords=False, enabled=False)
    monkeypatch.setattr(handlers, "is_running", Recorder(result=True))
    bot = FakeBot()
    msg = message(text="来玩博彩 https://spam.example @spammer")

    asyncio.run(spam_control.process_message(group_message_update(msg), filter_context(bot)))

    assert bot.deleted == [] and bot.sent == [] and punish.calls == []
    assert ai.calls == [((msg, bot), {})]


def test_with_the_filter_off_admins_are_not_sent_to_the_ai(monkeypatch):
    punish, ai = stub_filter(monkeypatch, keyword_hit=False, enabled=False)
    monkeypatch.setattr(handlers, "is_running", Recorder(result=True))
    context = filter_context(FakeBot())
    context.chat_data["is_admin:-100:7"] = True

    asyncio.run(spam_control.process_message(group_message_update(message(text="大家好")), context))

    assert ai.calls == []


def test_groups_with_nothing_on_skip_the_admin_lookup(monkeypatch):
    punish, ai = stub_filter(monkeypatch, keyword_hit=True, enabled=False)
    monkeypatch.setattr(handlers, "is_running", Recorder(result=False))
    lookup = Recorder()
    bot = FakeBot()
    bot.get_chat_member = lookup
    context = SimpleNamespace(bot=bot, chat_data={})

    asyncio.run(spam_control.process_message(group_message_update(message(text="来玩博彩")), context))

    assert lookup.calls == [] and ai.calls == [] and bot.deleted == []


@pytest.mark.parametrize(("configured", "checking", "running"), [(True, True, True), (False, True, False), (True, False, False)])
def test_the_ai_check_runs_only_when_configured_and_paid(monkeypatch, configured, checking, running):
    monkeypatch.setattr(judge, "is_configured", lambda: configured)
    monkeypatch.setattr(operations, "is_checking", Recorder(result=checking))

    assert asyncio.run(handlers.is_running(-100)) is running


@pytest.mark.parametrize(("ai_running", "extra"), [(False, False), (True, True)])
def test_turning_the_filter_off_says_whether_the_ai_check_keeps_running(ai_running, extra):
    text = spam_control_views.disabled_text(ai_running)

    assert text.startswith(spam_control_views.DISABLED_TEXT)
    assert ("AI 识别不受影响，仍在运行；如需暂停，使用 /spam ai off。" in text) is extra


@pytest.mark.parametrize(
    ("text", "link"),
    [
        ("看这个 https://example.com/a?b=1", "https://example.com/a?b=1"),
        ("www.abc.net 有好东西", "www.abc.net"),
        ("加我t.me/abc123", "t.me/abc123"),
        ("访问abc.xyz看看", "abc.xyz"),
        ("福利 spam.vip", "spam.vip"),
        ("sub.domain.co/path", "sub.domain.co/path"),
        ("联系 abc@gmail.com", "gmail.com"),
        ("Nice view.", None),
        ("版本 1.5.2 发布了", None),
        ("e.g. this one", None),
        ("改一下 config.py 和 node.js", None),
        ("看 readme.md", None),
        ("哈哈...好吧", None),
        ("abc.community", None),
    ],
)
def test_the_link_filter_only_matches_real_links(text, link):
    assert spam_rules.find_link(text) == link


def test_the_link_filter_catches_hidden_text_links(monkeypatch):
    stub_settings(monkeypatch, block_links=True)
    bot = FakeBot()
    msg = message(
        text="点击领取福利",
        entities=(MessageEntity(MessageEntity.TEXT_LINK, 0, 4, url="https://spam.example/<x>"),),
    )

    asyncio.run(spam_control.process_message(group_message_update(msg), filter_context(bot)))

    assert bot.deleted == [55]
    assert "<tg-spoiler>https://spam.example/&lt;x&gt;</tg-spoiler>" in bot.sent[0]
    assert "小美" not in bot.sent[0]
    assert [button.text for button in bot.markups[0].inline_keyboard[0]] == [
        admin_ban_views.VIEW_LABEL,
        admin_ban_views.BAN_LABEL,
    ]


def test_the_link_filter_catches_links_on_inline_bot_buttons(monkeypatch):
    stub_settings(monkeypatch, block_links=True)
    bot = FakeBot()

    asyncio.run(spam_control.process_message(group_message_update(inline_bot_ad()), filter_context(bot)))

    assert bot.deleted == [55]
    assert "<tg-spoiler>https://t.me/spamgroup</tg-spoiler>" in bot.sent[0]
    # 广告在按钮上，原文也记下按钮上的文字
    assert deleted_messages.ORIGINALS.get(-100, 55) == "加入福利群\n👍"


def test_button_labels_are_checked_against_spam_words(monkeypatch):
    seen = []

    async def find_spam_word(text, chat_id):
        seen.append(text)
        return "福利"

    punish, ai = stub_filter(monkeypatch, keyword_hit=True)
    monkeypatch.setattr(spam_rules, "find_spam_word", find_spam_word)
    bot = FakeBot()

    asyncio.run(spam_control.process_message(group_message_update(inline_bot_ad()), filter_context(bot)))

    assert seen == ["加入福利群\n👍"]
    assert bot.deleted == [55] and len(punish.calls) == 1 and ai.calls == []


def test_inline_bot_buttons_reach_the_ai_without_any_text(monkeypatch):
    punish, ai = stub_filter(monkeypatch, keyword_hit=False)
    bot = FakeBot()
    msg = inline_bot_ad()

    asyncio.run(spam_control.process_message(group_message_update(msg), filter_context(bot)))

    assert ai.calls == [((msg, bot), {})]


def test_an_inline_bot_message_with_nothing_to_read_is_skipped(monkeypatch):
    punish, ai = stub_filter(monkeypatch, keyword_hit=False)
    bot = FakeBot()

    asyncio.run(spam_control.process_message(group_message_update(inline_bot_ad(reply_markup=None)), filter_context(bot)))

    assert ai.calls == [] and bot.deleted == []


# ---------------------------------------------------------------------------
# 访客 bot（Bot API 10.0 Guest Mode）的回复与别的 bot 的消息
# ---------------------------------------------------------------------------

AD_BOT = SimpleNamespace(id=900, is_bot=True, username="Dhm_9ecded5dbot", full_name="Dhm 9ecded5d Bot")


def guest_reply(**fields):
    """成员 @ 出来的访客 bot 的回复：发送者是 bot，@ 它的人在 guest_bot_caller_user。"""
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("海外账户自助购买网站", url="https://spam.example/shop")]])
    values = dict(
        from_user=AD_BOT,
        guest_bot_caller_user=message().from_user,
        text="全网最大海外机房号商",
        reply_to_message=SimpleNamespace(text="@Dhm_9ecded5dbot", caption=None),
        reply_markup=keyboard,
    )
    values.update(fields)
    return message(**values)


def test_a_guest_bot_reply_counts_against_the_member_who_called_it():
    msg = guest_reply()

    assert spam_strikes.strike_target(msg) == 7
    assert spam_strikes.sender_html(msg) == admin_ban_views.neutral_mention(7)
    assert handlers.should_review(msg)
    data = handlers.review_input(msg)
    assert (data.sender_name, data.sender_username, data.via_bot) == ("小美", "xm", "Dhm_9ecded5dbot")
    assert (data.reply_to_text, data.hidden_links) == ("@Dhm_9ecded5dbot", ("https://spam.example/shop",))


def test_a_guest_bot_called_as_a_channel_has_nobody_to_punish():
    msg = guest_reply(guest_bot_caller_user=None, guest_bot_caller_chat=SimpleNamespace(title="某频道"))

    assert spam_strikes.strike_target(msg) is None
    assert spam_strikes.sender_html(msg) == "该频道"
    assert not handlers.should_review(msg)


@pytest.mark.parametrize(
    ("msg", "expected"),
    [
        (guest_reply(), True),
        (message(from_user=AD_BOT), True),
        (message(), False),
        (message(is_bot=True, sender_chat=SimpleNamespace(title="频道")), False),
    ],
    ids=["guest-reply", "bot", "person", "as-channel"],
)
def test_only_messages_bots_send_as_themselves_count_as_bot_messages(msg, expected):
    assert spam_control.SENT_BY_BOT.filter(msg) is expected


def test_a_guest_bot_ad_is_filtered_as_the_callers_message_and_goes_no_further(monkeypatch):
    stub_settings(monkeypatch, block_links=True)
    bot = FakeBot()

    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(spam_control.process_bot_message(group_message_update(guest_reply()), filter_context(bot)))

    assert bot.deleted == [55]
    assert "<tg-spoiler>https://spam.example/shop</tg-spoiler>" in bot.sent[0]
    assert admin_ban_views.neutral_mention(7) in bot.sent[0]
    assert buttons(bot.markups[0]) == [admin_ban_views.view_data(-100, 55), admin_ban_views.ban_data(-100, 7)]
    assert deleted_messages.ORIGINALS.get(-100, 55) == "全网最大海外机房号商\n海外账户自助购买网站"


def test_other_bot_messages_are_left_alone_and_go_no_further(monkeypatch):
    punish, ai = stub_filter(monkeypatch, keyword_hit=True)
    bot = FakeBot()

    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(spam_control.process_bot_message(group_message_update(message(from_user=AD_BOT)), filter_context(bot)))

    assert bot.deleted == [] and bot.sent == []
    assert punish.calls == [] and ai.calls == []
