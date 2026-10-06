"""X（Twitter）帖子的数据源：FxTwitter API v2 的用户时间线，不需要登录，也不需要 API key。

`GET {X_FEED_API_BASE}/2/profile/{handle}/statuses` 返回账号最近约 20 条帖子（含转帖与回复），
文档见 https://docs.fxembed.com/api/introduction ，限额 1000 次/分钟。返回的是 X 对未登录访客展示的时间线：
顺序不严格按时间（自己转帖的旧帖可能排在最前），所以调用方按帖子 id 排序与去重。

这里只负责请求与解析；同步哪些帖子由 `operations` 决定。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import aiohttp

from fogmoe_telegram_bot.core import config

REQUEST_TIMEOUT_SECONDS = 15
USER_AGENT = "fogmoe-telegram-bot (+https://github.com/FogMoe/telegram-bot)"

_HANDLE = re.compile(r"[A-Za-z0-9_]{1,15}")
# x.com/<handle>、twitter.com/<handle>，后面可以带 /status/... 或查询参数
_PROFILE_URL = re.compile(
    r"(?:https?://)?(?:www\.|mobile\.)?(?:x|twitter)\.com/(?P<handle>[A-Za-z0-9_]{1,15})(?:[/?#].*)?",
    re.IGNORECASE,
)
# 不是用户名的保留路径
_RESERVED_PATHS = {"home", "i", "search", "explore", "settings", "intent", "share", "messages"}


class XSourceError(Exception):
    """数据源暂时不可用：网络错误、超时或非预期的响应。"""


class XAccountNotFound(Exception):
    """账号不存在（或已被封禁、改名）。"""

    def __init__(self, handle: str) -> None:
        super().__init__(handle)
        self.handle = handle


@dataclass(frozen=True, slots=True)
class XPost:
    id: int
    author: str  # 作者的 screen_name
    is_reply: bool
    is_repost: bool  # 出现在时间线上是因为被这个账号转帖
    quoted_author: str | None = None
    created_at: float | None = None  # unix 秒；响应里没有时为 None

    @property
    def url(self) -> str:
        return f"https://x.com/{self.author}/status/{self.id}"


def normalize_handle(raw: str) -> str | None:
    """把 `@name`、`name` 或主页链接规范成用户名；不是合法用户名返回 None。"""
    value = raw.strip()
    match = _PROFILE_URL.fullmatch(value)
    if match:
        value = match.group("handle")
    value = value.removeprefix("@")
    if not _HANDLE.fullmatch(value) or value.lower() in _RESERVED_PATHS:
        return None
    return value


def _post_from_status(status: Mapping[str, Any]) -> XPost | None:
    try:
        post_id = int(status["id"])
        author = str(status["author"]["screen_name"])
    except (KeyError, TypeError, ValueError):
        return None
    quote = status.get("quote")
    quoted_author = None
    if isinstance(quote, Mapping):
        quote_author = quote.get("author")
        if isinstance(quote_author, Mapping) and quote_author.get("screen_name"):
            quoted_author = str(quote_author["screen_name"])
    created = status.get("created_timestamp")
    return XPost(
        id=post_id,
        author=author,
        is_reply=status.get("replying_to") is not None,
        is_repost=status.get("reposted_by") is not None,
        quoted_author=quoted_author,
        created_at=float(created) if isinstance(created, (int, float)) else None,
    )


def parse_statuses(payload: Any) -> list[XPost]:
    """解析时间线响应里的帖子；缺字段的条目跳过。"""
    results = payload.get("results") if isinstance(payload, Mapping) else None
    if not isinstance(results, list):
        return []
    posts = []
    for status in results:
        if isinstance(status, Mapping):
            post = _post_from_status(status)
            if post is not None:
                posts.append(post)
    return posts


def new_session() -> aiohttp.ClientSession:
    """一次绑定或一轮轮询共用的 HTTP 会话。"""
    return aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )


async def fetch_posts(session: aiohttp.ClientSession, handle: str) -> list[XPost]:
    """取账号最近的帖子。账号不存在抛 XAccountNotFound，其余失败抛 XSourceError。"""
    url = f"{config.X_FEED_API_BASE.rstrip('/')}/2/profile/{handle}/statuses"
    try:
        async with session.get(url) as response:
            status = response.status
            payload = await response.json(content_type=None)
    except (TimeoutError, aiohttp.ClientError, ValueError) as exc:
        raise XSourceError(f"请求 {handle} 的时间线失败: {type(exc).__name__}") from exc

    code = payload.get("code") if isinstance(payload, Mapping) else None
    if status == 404 or code == 404:
        raise XAccountNotFound(handle)
    if status != 200 or code != 200:
        raise XSourceError(f"{handle} 的时间线返回 HTTP {status}, code {code}")
    return parse_statuses(payload)
