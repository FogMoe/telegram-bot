"""admin 读取群管的群：app 注入 moderation 的读取函数，`/stats` 与 `/admin_announce` 只通过它读（不连数据库）。"""

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import OperationalError

from fogmoe_telegram_bot.app import handler_groups
from fogmoe_telegram_bot.core import config
from fogmoe_telegram_bot.features.admin import announce, developer, group_usage
from fogmoe_telegram_bot.features.moderation.operations import group_usage as moderation_groups
from fogmoe_telegram_bot.features.moderation.repositories import keyword_replies


@pytest.fixture(autouse=True)
def _restore_injection():
    before = group_usage.moderation_groups()
    yield
    group_usage.install(before)


def reader(count, group_ids, error=None):
    async def read(limit=None):
        read.calls.append(limit)
        if error is not None:
            raise error
        return count, group_ids

    read.calls = []
    return read


def install(**readers):
    empty = reader(0, [])
    values = {name: readers.get(name, empty) for name in ("keywords", "verification", "spam_filter", "spam_settings")}
    group_usage.install(group_usage.ModerationGroups(**values))


class Replies:
    def __init__(self):
        self.texts = []

    async def reply_text(self, text, **kwargs):
        self.texts.append(text)


def admin_update():
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=config.ADMIN_USER_ID),
        message=Replies(),
    )


def test_registering_admin_handlers_installs_the_moderation_readers():
    handler_groups.register_admin_handlers(SimpleNamespace(add_handler=lambda handler, group=0: None))

    installed = group_usage.moderation_groups()
    assert installed.keywords is moderation_groups.keyword_groups
    assert installed.verification is moderation_groups.verification_groups
    assert installed.spam_filter is moderation_groups.spam_filter_groups
    assert installed.spam_settings is moderation_groups.spam_settings_groups


def test_without_injection_there_are_no_moderation_groups():
    group_usage.install(group_usage.NOT_INSTALLED)

    assert asyncio.run(group_usage.moderation_groups().keywords(20)) == (0, [])


def test_a_limited_read_counts_every_group_and_lists_the_first_ones(monkeypatch):
    async def count_groups():
        return 42

    async def list_group_ids(limit=None):
        return [-1, -2][:limit]

    monkeypatch.setattr(keyword_replies, "count_groups", count_groups)
    monkeypatch.setattr(keyword_replies, "list_group_ids", list_group_ids)

    assert asyncio.run(moderation_groups.keyword_groups(1)) == (42, [-1])
    assert asyncio.run(moderation_groups.keyword_groups()) == (2, [-1, -2])


def test_stats_show_the_moderation_groups(monkeypatch):
    keywords = reader(2, [-1, -2])
    verification = reader(1, [-3])
    spam_filter = reader(0, [])
    install(keywords=keywords, verification=verification, spam_filter=spam_filter)

    async def fetch_one(sql, *args, **kwargs):
        return {"count": 7}

    async def fetch_all(sql, *args, **kwargs):
        return []

    monkeypatch.setattr(developer.mysql_connection, "fetch_one", fetch_one)
    monkeypatch.setattr(developer.mysql_connection, "fetch_all", fetch_all)
    update = admin_update()

    asyncio.run(developer.get_bot_stats.__wrapped__(update, SimpleNamespace()))

    (text,) = update.message.texts
    assert "💬 配置关键词群组: 2\n✅ 启用验证群组: 1\n🛡️ 启用垃圾控制群组: 0\n📈 配置图表群组: 7" in text
    assert "💬 关键词: `-1, -2`\n✅ 验证: `-3`\n🛡️ 垃圾控制: `无`" in text
    assert keywords.calls == verification.calls == spam_filter.calls == [20]


def test_announcements_reach_every_moderation_group_and_skip_a_failed_source(monkeypatch):
    keywords = reader(1, [-1])
    verification = reader(0, [], error=OperationalError("SELECT", {}, Exception("down")))
    spam_settings = reader(2, [-1, -3])
    install(keywords=keywords, verification=verification, spam_settings=spam_settings)

    async def fetch_all(sql, *args, **kwargs):
        if "FROM user" in sql:
            return [(10,)]
        return [(-5,)] if "group_chart_tokens" in sql else []

    async def no_wait(seconds):
        pass

    sent = []

    async def send_message(chat_id, text, **kwargs):
        sent.append(chat_id)

    monkeypatch.setattr(announce.mysql_connection, "fetch_all", fetch_all)
    monkeypatch.setattr(announce.asyncio, "sleep", no_wait)
    monkeypatch.setattr(announce, "ADMIN_USER_ID", config.ADMIN_USER_ID)
    update = admin_update()
    context = SimpleNamespace(args=["hello"], bot=SimpleNamespace(send_message=send_message))

    asyncio.run(announce.admin_announce.__wrapped__(update, context))

    assert sorted(sent) == [-5, -3, -1, 10]
    assert keywords.calls == verification.calls == spam_settings.calls == [None]
