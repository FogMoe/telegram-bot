"""/xfeed：数据源解析、选帖规则、消息格式与适配层的回复映射（不连网络、不连数据库）。

开通扣费与同步进度的事务语义在 tests/integration/test_xfeed_feeds.py。
"""

import asyncio
from types import SimpleNamespace

import pytest
from telegram.error import BadRequest, Forbidden, TimedOut

from fogmoe_telegram_bot.features.xfeed import handlers, operations, source
from fogmoe_telegram_bot.features.xfeed.operations import BindResult, BindStatus
from fogmoe_telegram_bot.features.xfeed.repositories.feeds import ActiveFeed, GroupFeed
from fogmoe_telegram_bot.features.xfeed.source import XPost


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


NOW = 1_800_000_000.0
HOUR = 3600


def post(post_id, *, author="alice", reply=False, repost=False, quoted=None, age=60):
    return XPost(
        post_id,
        author,
        is_reply=reply,
        is_repost=repost,
        quoted_author=quoted,
        created_at=NOW - age,
    )


# ---------------------------------------------------------------------------
# 数据源
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("elonmusk", "elonmusk"),
        ("@Elon_Musk", "Elon_Musk"),
        ("https://x.com/telegram", "telegram"),
        ("twitter.com/telegram/status/123?s=20", "telegram"),
        ("https://mobile.twitter.com/telegram/", "telegram"),
        ("x.com/home", None),
        ("bad-name", None),
        ("a" * 16, None),
        ("", None),
    ],
)
def test_normalize_handle(raw, expected):
    assert source.normalize_handle(raw) == expected


def test_parse_statuses_maps_reposts_replies_and_quotes():
    payload = {
        "code": 200,
        "results": [
            {"id": "3", "text": "own", "author": {"screen_name": "alice"},
             "media": {"photos": [{}]}, "replying_to": None, "reposted_by": None,
             "created_timestamp": 1791208802},
            {"id": "2", "text": "rt", "author": {"screen_name": "bob"},
             "reposted_by": {"screen_name": "alice"}},
            {"id": "4", "text": "re", "author": {"screen_name": "alice"},
             "replying_to": {"screen_name": "bob"}},
            {"id": "5", "text": "q", "author": {"screen_name": "alice"},
             "quote": {"author": {"screen_name": "carol"}}, "media": {"videos": [{}]}},
            {"id": "not-a-number", "author": {"screen_name": "alice"}},
        ],
    }

    posts = source.parse_statuses(payload)

    assert [(p.id, p.is_repost, p.is_reply, p.quoted_author) for p in posts] == [
        (3, False, False, None),
        (2, True, False, None),
        (4, False, True, None),
        (5, False, False, "carol"),
    ]
    assert posts[0].created_at == 1791208802 and posts[1].created_at is None
    assert posts[0].url == "https://x.com/alice/status/3"


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
        self.urls = []

    def get(self, url):
        self.urls.append(url)
        return self.response


def test_fetch_posts_requests_the_configured_api(settings_override):
    settings_override(X_FEED_API_BASE="https://fx.example/")
    session = FakeSession(FakeResponse(200, {"code": 200, "results": []}))

    assert asyncio.run(source.fetch_posts(session, "alice")) == []
    assert session.urls == ["https://fx.example/2/profile/alice/statuses"]


@pytest.mark.parametrize(
    ("status", "payload", "error"),
    [
        (404, {"code": 404, "results": []}, source.XAccountNotFound),
        (200, {"code": 404, "results": []}, source.XAccountNotFound),
        (500, {"code": 500}, source.XSourceError),
        (200, ["unexpected"], source.XSourceError),
    ],
)
def test_fetch_posts_errors(status, payload, error):
    with pytest.raises(error):
        asyncio.run(source.fetch_posts(FakeSession(FakeResponse(status, payload)), "alice"))


# ---------------------------------------------------------------------------
# 选帖规则
# ---------------------------------------------------------------------------


def plan(posts, last_seen_id, **kwargs):
    result = operations.plan_delivery(posts, last_seen_id, now=NOW, **kwargs)
    return [p.id for p in result.posts], result.skipped_up_to


def test_plan_keeps_originals_and_quotes_newer_than_the_progress():
    posts = [
        post(9, quoted="bob"),
        post(5),  # 已同步过
        post(7),
        post(8, reply=True),
        post(10, author="bob", repost=True),
        post(7),  # 时间线里重复出现
    ]

    assert plan(posts, 6) == ([7, 9], None)


def test_plan_keeps_only_the_newest_when_too_many_and_skips_past_the_rest():
    posts = [post(i) for i in range(1, 9)]

    assert plan(posts, 0, limit=3) == ([6, 7, 8], 5)


def test_plan_does_not_backfill_posts_older_than_a_day():
    posts = [post(6, age=30 * HOUR), post(7, age=25 * HOUR), post(8, age=2 * HOUR)]

    assert plan(posts, 5) == ([8], 7)
    assert plan(posts[:2], 5) == ([], 7)


def test_plan_sends_posts_without_a_timestamp():
    undated = XPost(9, "alice", is_reply=False, is_repost=False)

    assert plan([undated], 5) == ([9], None)


def test_the_starting_point_ignores_reposts_and_defaults_to_zero():
    assert operations.latest_own_post_id([post(5), post(8, reply=True), post(99, repost=True)]) == 8
    assert operations.latest_own_post_id([post(99, repost=True)]) == 0
    assert operations.latest_own_post_id([]) == 0


def test_canonical_handle_uses_the_authors_spelling():
    posts = [post(1, author="Bob", repost=True), post(2, author="Alice")]

    assert operations.canonical_handle(posts, "alice") == "Alice"
    assert operations.canonical_handle([], "alice") == "alice"


def test_activation_op_key_comes_from_the_command_message():
    assert operations.activation_op_key(-100, 7) == "xfeed:-100:7"


# ---------------------------------------------------------------------------
# 消息格式
# ---------------------------------------------------------------------------


def test_format_post_names_the_author_and_links_the_original():
    assert handlers.format_post(post(42, author="alice")) == (
        "<b>@alice</b> 发布了新帖子\nhttps://x.com/alice/status/42"
    )


def test_format_post_names_the_quoted_account():
    assert handlers.format_post(post(1, quoted="bob")) == (
        "<b>@alice</b> 引用了 @bob 的帖子\nhttps://x.com/alice/status/1"
    )


# ---------------------------------------------------------------------------
# 适配层
# ---------------------------------------------------------------------------


def raw(handler):
    while hasattr(handler, "__wrapped__"):
        handler = handler.__wrapped__
    return handler


def command(*args, chat_type="supergroup", status="administrator"):
    update = SimpleNamespace(
        message=SimpleNamespace(message_id=11, reply_text=Recorder()),
        effective_user=SimpleNamespace(id=7),
        effective_chat=SimpleNamespace(id=-100, type=chat_type),
    )
    context = SimpleNamespace(
        args=list(args),
        bot=SimpleNamespace(get_chat_member=Recorder(result=SimpleNamespace(status=status))),
    )
    return update, context


def replies(update):
    return [args[0] for args, _ in update.message.reply_text.calls]


def fake_fetch(monkeypatch, *, result=None, error=None):
    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(source, "new_session", Session)
    fetch = Recorder(result=result, errors=[error])
    monkeypatch.setattr(source, "fetch_posts", fetch)
    return fetch


def run_command(update, context):
    asyncio.run(raw(handlers.xfeed_command)(update, context))


def test_the_command_only_works_in_groups():
    update, context = command("bind", "alice", chat_type="private")

    run_command(update, context)

    assert "只能在群组中使用" in replies(update)[0]


def test_only_admins_can_bind(monkeypatch):
    bind = Recorder()
    monkeypatch.setattr(operations, "bind_feed", bind)
    update, context = command("bind", "alice", status="member")

    run_command(update, context)

    assert "只有群组管理员" in replies(update)[0]
    assert bind.calls == []


def test_bind_charges_through_the_operation_with_the_message_identity(monkeypatch):
    fake_fetch(monkeypatch, result=[post(30, author="Alice"), post(99, repost=True)])
    bind = Recorder(result=BindResult(BindStatus.BOUND, charged=True))
    monkeypatch.setattr(operations, "bind_feed", bind)
    update, context = command("bind", "@alice")

    run_command(update, context)

    (request,), _ = bind.calls[0]
    assert (request.chat_id, request.user_id, request.handle) == (-100, 7, "Alice")
    assert (request.latest_post_id, request.op_key) == (30, "xfeed:-100:11")
    assert "已扣除 100 金币" in replies(update)[0]


@pytest.mark.parametrize(
    ("error", "reply"),
    [
        (source.XAccountNotFound("alice"), "找不到 X 账号 @alice"),
        (source.XSourceError("down"), "本次没有扣费"),
    ],
)
def test_bind_does_not_charge_when_the_account_cannot_be_read(monkeypatch, error, reply):
    fake_fetch(monkeypatch, error=error)
    bind = Recorder()
    monkeypatch.setattr(operations, "bind_feed", bind)
    update, context = command("bind", "alice")

    run_command(update, context)

    assert reply in replies(update)[0]
    assert bind.calls == []


def test_bind_reports_an_insufficient_balance(monkeypatch):
    fake_fetch(monkeypatch, result=[])
    monkeypatch.setattr(
        operations, "bind_feed", Recorder(result=BindResult(BindStatus.INSUFFICIENT, balance_total=12))
    )
    update, context = command("bind", "alice")

    run_command(update, context)

    assert replies(update) == ["开通需要 100 金币，你当前只有 12 枚。"]


@pytest.mark.parametrize(
    ("enabled", "status"),
    [(True, "本群正在同步 @alice 的帖子。"), (False, "本群已开通 X 同步，目前已暂停（上次同步的是 @alice）。")],
)
def test_status_shows_the_bound_account(monkeypatch, enabled, status):
    feed = GroupFeed(-100, "alice", enabled=enabled, last_seen_id=5, paid_by=7)
    monkeypatch.setattr(operations, "get_group_feed", Recorder(result=feed))
    update, context = command()

    run_command(update, context)

    assert replies(update)[0].startswith(status + "\n")


@pytest.mark.parametrize(
    ("result", "extra"),
    [
        (BindResult(BindStatus.BOUND, replaced_handle="bob"), "已停止同步 @bob。"),
        (BindResult(BindStatus.BOUND, resumed=True), "从上次的进度继续，暂停期间的帖子只补发 24 小时内的。"),
    ],
)
def test_rebind_explains_what_happened_to_the_progress(monkeypatch, result, extra):
    fake_fetch(monkeypatch, result=[])
    monkeypatch.setattr(operations, "bind_feed", Recorder(result=result))
    update, context = command("bind", "alice")

    run_command(update, context)

    assert replies(update)[0].endswith("\n" + extra)


# ---------------------------------------------------------------------------
# 发送
# ---------------------------------------------------------------------------


def deliver(monkeypatch, posts, *, send_errors=()):
    monkeypatch.setattr(handlers.time, "time", lambda: NOW)
    recorded = Recorder()
    stopped = Recorder()
    monkeypatch.setattr(operations, "record_delivered", recorded)
    monkeypatch.setattr(operations, "stop_feed", stopped)
    send = Recorder(errors=send_errors)
    context = SimpleNamespace(bot=SimpleNamespace(send_message=send))
    feed = ActiveFeed(-100, "alice", 5)
    asyncio.run(handlers._deliver(context, feed, posts))
    return send, recorded, stopped


def test_deliver_sends_new_posts_in_order_and_records_the_last(monkeypatch):
    send, recorded, stopped = deliver(monkeypatch, [post(8), post(4), post(6)])

    assert [args for args, _ in send.calls] == [
        (-100, handlers.format_post(post(6))),
        (-100, handlers.format_post(post(8))),
    ]
    assert send.calls[0][1]["link_preview_options"].url == "https://x.com/alice/status/6"
    assert recorded.calls == [((ActiveFeed(-100, "alice", 5), 8), {})]
    assert stopped.calls == []


def test_posts_too_old_to_send_still_move_the_progress(monkeypatch):
    send, recorded, _ = deliver(monkeypatch, [post(6, age=48 * HOUR)])

    assert send.calls == []
    assert recorded.calls == [((ActiveFeed(-100, "alice", 5), 6), {})]


def test_a_transient_error_keeps_the_unsent_posts_for_the_next_poll(monkeypatch):
    _, recorded, _ = deliver(monkeypatch, [post(6), post(7)], send_errors=[None, TimedOut()])

    assert recorded.calls == [((ActiveFeed(-100, "alice", 5), 6), {})]


@pytest.mark.parametrize(
    "error",
    [
        Forbidden("bot was kicked from the supergroup chat"),
        BadRequest("Chat not found"),
        BadRequest("Not enough rights to send text messages to the chat"),
    ],
)
def test_a_bot_that_cannot_post_pauses_the_feed(monkeypatch, error):
    _, recorded, stopped = deliver(monkeypatch, [post(6)], send_errors=[error])

    assert stopped.calls == [((ActiveFeed(-100, "alice", 5),), {})]
    assert recorded.calls == []


def test_a_rejected_post_is_skipped_instead_of_retried_forever(monkeypatch):
    send, recorded, stopped = deliver(
        monkeypatch, [post(6), post(7)], send_errors=[BadRequest("Message is too long"), None]
    )

    assert len(send.calls) == 2 and stopped.calls == []
    assert recorded.calls == [((ActiveFeed(-100, "alice", 5), 7), {})]


def test_nothing_new_sends_nothing(monkeypatch):
    send, recorded, _ = deliver(monkeypatch, [post(5), post(3)])

    assert send.calls == [] and recorded.calls == []
