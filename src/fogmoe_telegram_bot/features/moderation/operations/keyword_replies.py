"""群关键词自动回复的业务规则：添加与删除、HTML 净化、触发词匹配、触发频率与缓存。不依赖 Telegram。

规则：

- 每个群最多 `MAX_REPLIES` 个关键词；触发词不超过 `MAX_KEYWORD_LENGTH` 个字符，回复不超过 `MAX_RESPONSE_LENGTH` 个字符。
- 回复内容是 Telegram HTML，白名单之外的标签转义后保存，发送前再净化一次。
- 一条消息只回复第一个匹配的关键词。每个群每分钟最多回复 `MAX_TRIGGERS_PER_MINUTE` 次，
  只有命中关键词的消息占用额度；额度用完时命中的消息不回复。

每个群的关键词缓存 `CACHE_SECONDS` 秒，本进程里的修改立即刷新缓存。缓存与触发记录都在进程内存里，重启后清空。
"""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from enum import StrEnum

from fogmoe_telegram_bot.core import sql

from ..repositories import keyword_replies
from ..repositories.keyword_replies import KeywordReply

logger = logging.getLogger(__name__)

ALLOWED_HTML_TAGS = {
    "b", "i", "u", "a", "code", "pre", "s", "strong", "em",
    "ins", "del", "strike", "span", "tg-spoiler",
}
MAX_RESPONSE_LENGTH = 1000
MAX_KEYWORD_LENGTH = 255
MAX_REPLIES = 10
CACHE_SECONDS = 300
MAX_TRIGGERS_PER_MINUTE = 5


def sanitize_html(html_content: str) -> str:
    """转义白名单之外的 HTML 标签。"""
    tags = re.findall(r"</?([a-zA-Z0-9]+)[^>]*>", html_content)
    for tag in set(tags):
        if tag.lower() not in ALLOWED_HTML_TAGS:
            html_content = re.sub(
                r"<(/?)" + re.escape(tag) + r"([^>]*)>",
                r"&lt;\1" + tag + r"\2&gt;",
                html_content,
            )
    return html_content


def is_keyword_match(keyword: str, message: str) -> bool:
    """按单词边界匹配；含中文等不用空格分词的文字时退回子串匹配。"""
    if re.search(r"\b" + re.escape(keyword) + r"\b", message, re.IGNORECASE):
        return True
    if not re.match(r"^[a-zA-Z0-9\s\W]+$", keyword):
        return keyword.lower() in message.lower()
    return False


# ---------------------------------------------------------------------------
# 缓存与触发频率
# ---------------------------------------------------------------------------

# {chat_id: (读取时刻, 关键词)}
_cache: dict[int, tuple[float, list[KeywordReply]]] = {}
# {chat_id: [最近一分钟内的触发时刻]}
_triggers: defaultdict[int, list[float]] = defaultdict(list)


async def _load_replies(chat_id: int) -> list[KeywordReply]:
    """从数据库读取并刷新缓存；读取失败时当作没有关键词，也不写缓存。"""
    try:
        replies = await keyword_replies.list_replies(chat_id)
    except Exception as exc:
        logger.error("从数据库加载关键词时出错: %s", exc)
        return []
    _cache[chat_id] = (time.monotonic(), replies)
    return replies


async def get_replies(chat_id: int) -> list[KeywordReply]:
    cached = _cache.get(chat_id)
    if cached is not None and time.monotonic() - cached[0] < CACHE_SECONDS:
        return cached[1]
    return await _load_replies(chat_id)


def take_trigger(chat_id: int) -> bool:
    """占用一次这一分钟的触发额度；额度用完返回 False。"""
    now = time.monotonic()
    triggers = _triggers[chat_id]
    while triggers and now - triggers[0] > 60:
        triggers.pop(0)
    if len(triggers) >= MAX_TRIGGERS_PER_MINUTE:
        return False
    triggers.append(now)
    return True


async def find_reply(chat_id: int, text: str) -> KeywordReply | None:
    """这条消息要回复的关键词；没有匹配、或这一分钟的额度已经用完时返回 None。"""
    for reply in await get_replies(chat_id):
        if is_keyword_match(reply.keyword, text):
            return reply if take_trigger(chat_id) else None
    return None


# ---------------------------------------------------------------------------
# 添加与删除
# ---------------------------------------------------------------------------


class AddReplyStatus(StrEnum):
    ADDED = "added"
    UPDATED = "updated"
    KEYWORD_TOO_LONG = "keyword_too_long"
    RESPONSE_TOO_LONG = "response_too_long"
    LIMIT_REACHED = "limit_reached"


async def add_reply(chat_id: int, user_id: int, keyword: str, response: str) -> AddReplyStatus:
    """添加关键词回复；同一个触发词已经存在时更新回复内容。数据库出错时抛出。"""
    if len(keyword) > MAX_KEYWORD_LENGTH:
        return AddReplyStatus.KEYWORD_TOO_LONG
    if len(response) > MAX_RESPONSE_LENGTH:
        return AddReplyStatus.RESPONSE_TOO_LONG
    response = sanitize_html(response)

    async with sql.transaction() as connection:
        exists = await keyword_replies.reply_exists(chat_id, keyword, connection=connection)
        if not exists:
            # 缓存里已经满了就不必再查数据库
            cached = _cache.get(chat_id)
            if cached is not None and len(cached[1]) >= MAX_REPLIES:
                return AddReplyStatus.LIMIT_REACHED
            if await keyword_replies.count_replies(chat_id, connection=connection) >= MAX_REPLIES:
                return AddReplyStatus.LIMIT_REACHED
        await keyword_replies.upsert_reply(
            connection, chat_id, keyword, response, user_id=user_id
        )
    await _load_replies(chat_id)
    return AddReplyStatus.UPDATED if exists else AddReplyStatus.ADDED


async def delete_reply(chat_id: int, keyword: str) -> bool:
    """删除关键词回复，没有这个触发词时返回 False。数据库出错时抛出。"""
    async with sql.transaction() as connection:
        deleted = await keyword_replies.delete_reply(connection, chat_id, keyword)
    if deleted:
        await _load_replies(chat_id)
    return deleted
