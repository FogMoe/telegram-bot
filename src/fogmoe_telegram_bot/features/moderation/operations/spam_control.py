"""垃圾信息过滤的业务规则：群开关、自定义垃圾词、全局垃圾词库、链接与 @提及识别、警告计数。不依赖 Telegram。

规则：

- 链接过滤与 @提及过滤要在总开关打开之后才能设置；适配层先用 `is_enabled` 检查。
- 每个群最多 `MAX_CUSTOM_KEYWORDS` 个自定义垃圾词，每个不超过 `MAX_KEYWORD_LENGTH` 个字符，`//` 开头的是正则表达式。
- 群里有（能编译的）自定义垃圾词时只用自定义的，全局垃圾词库不生效。
- 链接、@提及与全局垃圾词库的命中只警告，警告次数按（群, 用户）累计，每 `WARNING_RESET_SECONDS` 秒整体清零；
  自定义垃圾词命中的累计处罚在 `moderation/spam_strikes.py`。

群开关与自定义垃圾词各缓存 `CACHE_SECONDS` 秒，本进程里的修改立即写进缓存；全局垃圾词库按文件的修改时间重新加载。
这些状态都在进程内存里，重启后从数据库与文件重新读取，警告次数随重启清零。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

from fogmoe_telegram_bot.core import config, sql

from ..repositories import spam_keywords, spam_settings
from ..repositories.spam_keywords import SpamKeyword
from ..repositories.spam_settings import SpamSettings

logger = logging.getLogger(__name__)

CACHE_SECONDS = 300
MAX_CUSTOM_KEYWORDS = 10
MAX_KEYWORD_LENGTH = 255
REGEX_PREFIX = "//"
WARNING_RESET_SECONDS = 3600
SPAM_FILE_NAME = "spam_words.txt"
SPAM_FILE_UPDATE_INTERVAL = 600  # 全局垃圾词库的修改时间早于这么久时，每次检查前都看一眼文件有没有更新

# 没有记录的群：全部关闭
ALL_OFF = SpamSettings(enabled=False, block_links=False, block_mentions=False)

# 只认明确的链接写法：带协议、www. 开头，或「域名.常见后缀」（可以接路径）。
# 后缀限定在这张表里，「Nice view.」「1.5」「config.py」这类带点的普通文字不算链接；
# 前后用 ASCII 字符类而不是 \b，因为中文也算 \w，「访问abc.com看看」里的域名两侧没有 \b。
LINK_TLDS = (
    "com", "net", "org", "info", "biz", "pro", "io", "co", "me", "cc", "tv", "ai", "app", "dev",
    "xyz", "top", "vip", "icu", "cyou", "buzz", "club", "shop", "site", "online", "store", "tech",
    "live", "fun", "win", "bet", "link", "ly", "gg", "tk", "ml", "ga", "cf", "gq", "pw", "ws", "la",
    "cn", "hk", "tw", "jp", "kr", "sg", "ru", "uk", "de", "wang", "xin", "ren", "gov", "edu",
)
URL_PATTERN = re.compile(
    r"https?://\S+"
    r"|www\.\S+"
    r"|(?<![a-z0-9.-])(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+(?:" + "|".join(LINK_TLDS) + r")"
    r"(?![a-z0-9-])(?:[/?#]\S*)?",
    re.IGNORECASE,
)
# Telegram 的 @username
MENTION_PATTERN = re.compile(r"@[a-zA-Z0-9_]+")


# ---------------------------------------------------------------------------
# 群开关
# ---------------------------------------------------------------------------

# {chat_id: (读取时刻, 开关)}
_settings_cache: dict[int, tuple[float, SpamSettings]] = {}


async def load_settings(chat_id: int) -> SpamSettings:
    """从数据库读取群的开关并刷新缓存；读取失败时抛出。"""
    try:
        settings = await spam_settings.get_settings(chat_id) or ALL_OFF
    except Exception as exc:
        logger.error("加载垃圾信息过滤状态时出错: %s", exc)
        raise
    _settings_cache[chat_id] = (time.monotonic(), settings)
    return settings


async def get_settings(chat_id: int) -> SpamSettings:
    cached = _settings_cache.get(chat_id)
    if cached is not None and time.monotonic() - cached[0] < CACHE_SECONDS:
        return cached[1]
    return await load_settings(chat_id)


async def is_enabled(chat_id: int) -> bool:
    return (await get_settings(chat_id)).enabled


def _update_cached(chat_id: int, **changes: bool) -> None:
    """改写已经缓存的开关并重新计时；没有缓存时不动，下次读取时从数据库加载。"""
    cached = _settings_cache.get(chat_id)
    if cached is not None:
        _settings_cache[chat_id] = (time.monotonic(), replace(cached[1], **changes))


async def set_enabled(chat_id: int, user_id: int, enabled: bool) -> None:
    """打开或关闭总开关并记下操作的管理员；链接与 @提及过滤保持原样。"""
    async with sql.transaction() as connection:
        current = await spam_settings.get_settings(chat_id, connection=connection) or ALL_OFF
        await spam_settings.save_enabled(
            connection,
            chat_id,
            enabled=enabled,
            block_links=current.block_links,
            block_mentions=current.block_mentions,
            user_id=user_id,
        )
    if chat_id in _settings_cache:
        _update_cached(chat_id, enabled=enabled)
    else:
        _settings_cache[chat_id] = (time.monotonic(), replace(current, enabled=enabled))


async def set_link_blocking(chat_id: int, enabled: bool) -> None:
    async with sql.transaction() as connection:
        await spam_settings.set_block_links(connection, chat_id, enabled)
    _update_cached(chat_id, block_links=enabled)


async def set_mention_blocking(chat_id: int, enabled: bool) -> None:
    async with sql.transaction() as connection:
        await spam_settings.set_block_mentions(connection, chat_id, enabled)
    _update_cached(chat_id, block_mentions=enabled)


# ---------------------------------------------------------------------------
# 自定义垃圾词
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CustomKeywords:
    words: tuple[str, ...]  # 小写
    patterns: tuple[re.Pattern[str], ...]

    def __bool__(self) -> bool:
        return bool(self.words or self.patterns)


NO_CUSTOM_KEYWORDS = CustomKeywords((), ())

# {chat_id: (读取时刻, 垃圾词)}
_custom_cache: dict[int, tuple[float, CustomKeywords]] = {}
# 正在从数据库加载的群
_custom_loading: set[int] = set()


async def _load_custom_keywords(chat_id: int) -> CustomKeywords:
    """从数据库读取并刷新缓存；读取失败时当作没有自定义垃圾词，也不写缓存。"""
    try:
        rows = await spam_keywords.list_keywords(chat_id)
    except Exception as exc:
        logger.error("加载自定义垃圾词时出错: %s", exc)
        return NO_CUSTOM_KEYWORDS
    words: list[str] = []
    patterns: list[re.Pattern[str]] = []
    for row in rows:
        if not row.is_regex:
            words.append(row.keyword.lower())
            continue
        try:
            patterns.append(re.compile(row.keyword, re.IGNORECASE))
        except re.error:
            logger.error("无效的自定义正则表达式: %s", row.keyword)
    loaded = CustomKeywords(tuple(words), tuple(patterns))
    _custom_cache[chat_id] = (time.monotonic(), loaded)
    return loaded


def _cached_custom_keywords(chat_id: int, now: float) -> CustomKeywords | None:
    cached = _custom_cache.get(chat_id)
    if cached is not None and now - cached[0] < CACHE_SECONDS:
        return cached[1]
    return None


async def get_custom_keywords(chat_id: int) -> CustomKeywords:
    now = time.monotonic()
    cached = _cached_custom_keywords(chat_id, now)
    if cached is not None:
        return cached
    # 另一个协程正在加载这个群：稍等一下，多半可以直接用它加载的结果
    if chat_id in _custom_loading:
        await asyncio.sleep(0.1)
        cached = _cached_custom_keywords(chat_id, now)
        if cached is not None:
            return cached
    _custom_loading.add(chat_id)
    try:
        return await _load_custom_keywords(chat_id)
    finally:
        _custom_loading.discard(chat_id)


async def has_custom_keywords(chat_id: int) -> bool:
    return bool(await get_custom_keywords(chat_id))


def parse_keyword(raw: str) -> tuple[str, bool]:
    """命令里写的垃圾词 → (存进数据库的词, 是否正则)；`//` 开头的是正则表达式。"""
    if raw.startswith(REGEX_PREFIX):
        return raw[len(REGEX_PREFIX):].strip(), True
    return raw, False


class AddKeywordStatus(StrEnum):
    ADDED = "added"
    UPDATED = "updated"
    INVALID_REGEX = "invalid_regex"
    TOO_LONG = "too_long"
    LIMIT_REACHED = "limit_reached"


@dataclass(frozen=True, slots=True)
class AddKeywordResult:
    status: AddKeywordStatus
    keyword: str  # 去掉 `//` 前缀之后的词


async def add_custom_keyword(chat_id: int, user_id: int, raw_keyword: str) -> AddKeywordResult:
    """添加垃圾词；同一个词已经存在时更新它。数据库出错时抛出。"""
    keyword, is_regex = parse_keyword(raw_keyword)
    if is_regex:
        try:
            re.compile(keyword)
        except re.error:
            return AddKeywordResult(AddKeywordStatus.INVALID_REGEX, keyword)
    if len(keyword) > MAX_KEYWORD_LENGTH:
        return AddKeywordResult(AddKeywordStatus.TOO_LONG, keyword)

    async with sql.transaction() as connection:
        exists = await spam_keywords.keyword_exists(chat_id, keyword, connection=connection)
        if not exists:
            count = await spam_keywords.count_keywords(chat_id, connection=connection)
            if count >= MAX_CUSTOM_KEYWORDS:
                return AddKeywordResult(AddKeywordStatus.LIMIT_REACHED, keyword)
        await spam_keywords.upsert_keyword(
            connection, chat_id, keyword, is_regex=is_regex, user_id=user_id
        )
    await _load_custom_keywords(chat_id)
    status = AddKeywordStatus.UPDATED if exists else AddKeywordStatus.ADDED
    return AddKeywordResult(status, keyword)


@dataclass(frozen=True, slots=True)
class DeleteKeywordResult:
    deleted: bool
    keyword: str  # 去掉 `//` 前缀之后的词


async def delete_custom_keyword(chat_id: int, raw_keyword: str) -> DeleteKeywordResult:
    """删除垃圾词；数据库出错时抛出。"""
    keyword, _ = parse_keyword(raw_keyword)
    async with sql.transaction() as connection:
        deleted = await spam_keywords.delete_keyword(connection, chat_id, keyword)
    if deleted:
        await _load_custom_keywords(chat_id)
    return DeleteKeywordResult(deleted, keyword)


async def list_custom_keywords(chat_id: int) -> list[SpamKeyword]:
    """群里的自定义垃圾词（直接读数据库，不走缓存）。"""
    return await spam_keywords.list_keywords(chat_id)


# ---------------------------------------------------------------------------
# 全局垃圾词库：resources/spam_words.txt，一行一个词，`//` 开头的是正则表达式，`#` 开头的是注释
# ---------------------------------------------------------------------------

_global_words: set[str] = set()
_global_patterns: list[re.Pattern[str]] = []
_global_loaded_mtime = 0.0  # 已加载的文件修改时间


def spam_file_path() -> Path:
    return config.RESOURCES_DIR / SPAM_FILE_NAME


def _create_default_spam_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("# 垃圾词列表，一行一个词语\n")
        f.write("博彩\n发财\n")
        f.write("# 使用//开头的行表示正则表达式匹配模式\n")
        f.write("//\\d+\\s*[元块]\\s*[充值提现]\n")


def load_spam_words() -> None:
    """文件有更新时重新加载全局垃圾词库；文件不存在时先写一份默认的。"""
    global _global_words, _global_patterns, _global_loaded_mtime

    path = spam_file_path()
    if not os.path.exists(path):
        logger.warning("垃圾词列表文件未找到: %s", path)
        _create_default_spam_file(path)
        logger.info("已创建默认垃圾词列表文件: %s", path)

    file_mtime = os.path.getmtime(path)
    if file_mtime <= _global_loaded_mtime:
        return

    try:
        words: set[str] = set()
        patterns: list[re.Pattern[str]] = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith(REGEX_PREFIX):
                    pattern = line[len(REGEX_PREFIX):].strip()
                    try:
                        patterns.append(re.compile(pattern, re.IGNORECASE))
                    except re.error:
                        logger.error("无效的正则表达式: %s", pattern)
                else:
                    words.add(line.lower())
        _global_words = words
        _global_patterns = patterns
        _global_loaded_mtime = file_mtime
        logger.info("已加载 %s 个垃圾词和 %s 个正则表达式模式", len(words), len(patterns))
    except Exception as exc:
        logger.error("加载垃圾词列表时出错: %s", exc)


# ---------------------------------------------------------------------------
# 消息检查
# ---------------------------------------------------------------------------


def find_link(text: str | None) -> str | None:
    """文本里的第一个链接。"""
    if not text:
        return None
    match = URL_PATTERN.search(text)
    return match.group(0) if match else None


def find_mention(text: str | None) -> str | None:
    """文本里的第一个 @username。"""
    if not text:
        return None
    match = MENTION_PATTERN.search(text)
    return match.group(0) if match else None


def _match_words(
    text: str, words: Iterable[str], patterns: Iterable[re.Pattern[str]]
) -> str | None:
    text_lower = text.lower()
    for word in words:
        if word in text_lower:
            return word
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            # 匹配到空串时报出正则本身
            return match.group(0) or pattern.pattern
    return None


async def find_spam_word(text: str | None, chat_id: int) -> str | None:
    """命中的垃圾词；群里有自定义垃圾词时只用自定义的，否则用全局垃圾词库。"""
    if not text:
        return None
    custom = await get_custom_keywords(chat_id)
    if custom:
        return _match_words(text, custom.words, custom.patterns)
    # 比较的是文件的修改时间，所以用墙上时钟
    if time.time() - _global_loaded_mtime > SPAM_FILE_UPDATE_INTERVAL:
        load_spam_words()
    return _match_words(text, _global_words, _global_patterns)


# ---------------------------------------------------------------------------
# 警告计数（链接、@提及、全局垃圾词库）
# ---------------------------------------------------------------------------

# {chat_id: {user_id: 次数}}；清零在定时线程里执行，所以加锁
_warnings: defaultdict[int, defaultdict[int, int]] = defaultdict(lambda: defaultdict(int))
_warnings_lock = threading.Lock()


def add_warning(chat_id: int, user_id: int) -> int:
    """记一次警告，返回累计次数（含这一次）。"""
    with _warnings_lock:
        _warnings[chat_id][user_id] += 1
        return _warnings[chat_id][user_id]


def reset_warnings() -> None:
    with _warnings_lock:
        _warnings.clear()
    logger.info("已清理垃圾信息警告计数器")
