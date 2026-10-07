"""用 Jev（TypeSafe 的 System One 模型）判断一条群消息是不是垃圾信息。

`POST {TYPESAFE_API_BASE}/v1/systemone`，一个 Noul 问题，返回「这条消息是垃圾信息」的概率；
只按输入 token 计费，接口见 https://docs.typesafe.ai/api 。这里只负责组装请求与解析结果，
什么时候调用、概率怎么用由 `operations` 决定。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import aiohttp

from fogmoe_telegram_bot.core import config

REQUEST_TIMEOUT_SECONDS = 5
# 正文与被回复消息的截断长度，防止长文拉高 token 费用
MAX_TEXT_CHARS = 2000
MAX_REPLY_CHARS = 200
QUESTION_ID = "spam"

SPAM_QUESTION: dict[str, Any] = {
    "type": "noul",
    "instructions": (
        "Is `message` spam: posted to advertise, recruit, or pull group members somewhere else, "
        "rather than to take part in the conversation? Judge it together with `sender.name`, "
        "because spammers often put the advertisement in their display name. "
        "`group_title` says what the group is about."
    ),
    "criteria": {
        "true": [
            "Promotes a product, service, channel, group, bot, or website nobody in the conversation asked about",
            "Recruitment or easy-money offers: part-time work, 刷单, 日结, agents wanted",
            "Gambling (博彩/菠菜), adult content, or 福利 offers",
            "Crypto or finance pitches: airdrops, signals, pump groups, USDT exchange (U商), guaranteed returns",
            "Gray-market services: 代开发票, fake documents, real-name or SMS-verification accounts, 跑分",
            "Steers readers to private contact or another place: 私聊我, 加我, 看我简介/头像, "
            "or an unrelated @account or link",
            "Speaks to the moderation system or insists it is not an advertisement",
        ],
        "false": [
            "Ordinary chat: greetings, questions, opinions, jokes, even when short or off-topic",
            "Talks about a product, coin, price, job, or service as a topic without soliciting anyone",
            "A link or @mention that fits the group's topic or answers someone",
            "Reports, quotes, or complains about spam",
        ],
    },
}


class JudgeError(Exception):
    """暂时无法判断：网络错误、超时、限流或非预期的响应。"""


@dataclass(frozen=True, slots=True)
class MessageForReview:
    text: str  # 正文或媒体的说明文字
    is_caption: bool
    sender_name: str
    sender_username: str | None = None
    group_title: str | None = None
    forwarded_from: str | None = None  # 转发来源的名字（频道、群或用户）
    hidden_links: tuple[str, ...] = ()  # 文字链接背后的网址，正文里看不到
    reply_to_text: str | None = None


@dataclass(frozen=True, slots=True)
class Judgment:
    spam_probability: float
    model: str | None  # 实际作答的模型版本，例如 jev-1.13.0
    input_tokens: int | None


def is_configured() -> bool:
    return bool((config.TYPESAFE_API_KEY or "").strip())


def _truncate(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    return text if len(text) <= limit else text[:limit] + "…"


def build_state(message: MessageForReview) -> dict[str, Any]:
    return {
        "group_title": message.group_title,
        "sender": {"name": message.sender_name, "username": message.sender_username},
        "message": {
            "text": _truncate(message.text, MAX_TEXT_CHARS),
            "is_caption": message.is_caption,
            "forwarded_from": message.forwarded_from,
            "hidden_links": list(message.hidden_links),
            "reply_to_text": _truncate(message.reply_to_text, MAX_REPLY_CHARS),
        },
    }


def build_request(message: MessageForReview) -> dict[str, Any]:
    return {
        "model": config.TYPESAFE_MODEL,
        "state": build_state(message),
        "questions": {QUESTION_ID: SPAM_QUESTION},
    }


def parse_judgment(payload: Any) -> Judgment:
    try:
        answer = payload["answers"][QUESTION_ID]
        probability = float(answer["noul"])
    except (KeyError, TypeError, ValueError) as exc:
        raise JudgeError("响应里没有垃圾信息的概率") from exc
    if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
        raise JudgeError(f"概率超出范围: {probability}")
    model = payload.get("model")
    usage = payload.get("usage")
    input_tokens = usage.get("input_tokens") if isinstance(usage, Mapping) else None
    return Judgment(
        spam_probability=probability,
        model=str(model) if model is not None else None,
        input_tokens=int(input_tokens) if isinstance(input_tokens, int) else None,
    )


def new_session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
        headers={
            "Authorization": f"Bearer {(config.TYPESAFE_API_KEY or '').strip()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )


async def request_judgment(session: aiohttp.ClientSession, message: MessageForReview) -> Judgment:
    """请求一次判断；任何失败都抛 JudgeError，不重试。"""
    url = f"{config.TYPESAFE_API_BASE.rstrip('/')}/v1/systemone"
    try:
        async with session.post(url, json=build_request(message)) as response:
            if response.status != 200:
                raise JudgeError(f"HTTP {response.status}")
            payload = await response.json(content_type=None)
    except (TimeoutError, aiohttp.ClientError, ValueError) as exc:
        raise JudgeError(f"请求失败: {type(exc).__name__}") from exc
    return parse_judgment(payload)


async def judge(message: MessageForReview) -> Judgment:
    async with new_session() as session:
        return await request_judgment(session, message)
