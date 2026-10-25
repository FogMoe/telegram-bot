"""群管的业务规则：垃圾词的解析与匹配、关键词回复的校验与触发频率（不连数据库）。"""

import asyncio

import pytest

from fogmoe_telegram_bot.features.moderation.operations import keyword_replies
from fogmoe_telegram_bot.features.moderation.operations import spam_control as spam_rules
from fogmoe_telegram_bot.features.moderation.operations.keyword_replies import AddReplyStatus
from fogmoe_telegram_bot.features.moderation.operations.spam_control import (
    AddKeywordStatus,
    CustomKeywords,
)
from fogmoe_telegram_bot.features.moderation.repositories.keyword_replies import KeywordReply


@pytest.fixture(autouse=True)
def _reset_memory_state():
    keyword_replies._triggers.clear()
    yield
    keyword_replies._triggers.clear()


def stub_custom_keywords(monkeypatch, custom):
    async def get_custom_keywords(chat_id):
        return custom

    monkeypatch.setattr(spam_rules, "get_custom_keywords", get_custom_keywords)


def test_a_double_slash_marks_a_regex():
    assert spam_rules.parse_keyword("// \\d+元 ") == ("\\d+元", True)
    assert spam_rules.parse_keyword("博彩") == ("博彩", False)


@pytest.mark.parametrize(
    ("raw", "status"),
    [("//[", AddKeywordStatus.INVALID_REGEX), ("x" * 256, AddKeywordStatus.TOO_LONG)],
)
def test_invalid_spam_keywords_are_refused_before_touching_the_database(raw, status):
    result = asyncio.run(spam_rules.add_custom_keyword(-100, 7, raw))

    assert result.status is status


def test_custom_keywords_replace_the_global_word_list(monkeypatch):
    stub_custom_keywords(monkeypatch, CustomKeywords(("兼职",), ()))
    monkeypatch.setattr(spam_rules, "_global_words", {"博彩"})

    assert asyncio.run(spam_rules.find_spam_word("来玩博彩", -100)) is None
    assert asyncio.run(spam_rules.find_spam_word("日结兼职", -100)) == "兼职"


def test_an_empty_regex_match_reports_the_pattern(monkeypatch):
    stub_custom_keywords(monkeypatch, CustomKeywords((), (spam_rules.re.compile("x*"),)))

    assert asyncio.run(spam_rules.find_spam_word("hello", -100)) == "x*"


def test_tags_outside_the_whitelist_are_escaped():
    assert keyword_replies.sanitize_html("<b>hi</b><script>x</script>") == (
        "<b>hi</b>&lt;script&gt;x&lt;/script&gt;"
    )


@pytest.mark.parametrize(
    ("keyword", "response", "status"),
    [
        ("k" * 256, "hi", AddReplyStatus.KEYWORD_TOO_LONG),
        ("hi", "r" * 1001, AddReplyStatus.RESPONSE_TOO_LONG),
    ],
)
def test_oversized_keyword_replies_are_refused(keyword, response, status):
    assert asyncio.run(keyword_replies.add_reply(-100, 7, keyword, response)) is status


def test_every_message_in_a_group_with_keywords_uses_the_trigger_budget(monkeypatch):
    reply = KeywordReply("hello", "<b>hi</b>")

    async def get_replies(chat_id):
        return [reply]

    monkeypatch.setattr(keyword_replies, "get_replies", get_replies)

    for _ in range(keyword_replies.MAX_TRIGGERS_PER_MINUTE):
        assert asyncio.run(keyword_replies.find_reply(-100, "无关的消息")) is None
    assert asyncio.run(keyword_replies.find_reply(-100, "hello there")) is None
    assert asyncio.run(keyword_replies.find_reply(-200, "hello there")) == reply
