"""AI 相关用户侧硬币定价与长度上限的唯一声明。

聊天消息（`features/conversation/billing.py`）、`/tl` 翻译命令（`features/ai/translate_handlers.py`）
以及提示词/内部文档的定价占位符渲染（`core/config.py`）都从这里读取，避免硬币阶梯或长度上限散落在多个模块与
Markdown 文本里各自漂移。
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

# 主对话文字消息：(长度下限（不含）, 硬币数)，从高到低匹配；不超过最低一档按 CHAT_MIN_TEXT_COST 计费。
CHAT_MIN_TEXT_COST = 1
CHAT_TEXT_COST_TIERS: tuple[tuple[int, int], ...] = (
    (2000, 5),
    (1000, 4),
    (500, 3),
    (100, 2),
)
CHAT_MAX_TEXT_COST = max(
    (cost for _, cost in CHAT_TEXT_COST_TIERS),
    default=CHAT_MIN_TEXT_COST,
)
CHAT_MEDIA_COST = 5
CHAT_MAX_MESSAGE_COST = max(CHAT_MAX_TEXT_COST, CHAT_MEDIA_COST)
CHAT_MAX_TEXT_LENGTH = 4096

# /tl 翻译命令：(长度下限（不含）, 硬币数)，从高到低匹配；不超过最低一档免费。
TRANSLATE_FREE_COST = 0
TRANSLATE_FREE_MAX_LENGTH = 500
TRANSLATE_COST_TIERS: tuple[tuple[int, int], ...] = (
    (2000, 3),
    (1000, 2),
    (TRANSLATE_FREE_MAX_LENGTH, 1),
)
TRANSLATE_MAX_COST = max(
    (cost for _, cost in TRANSLATE_COST_TIERS),
    default=TRANSLATE_FREE_COST,
)
TRANSLATE_MAX_TEXT_LENGTH = 3000

PRICING_PLACEHOLDERS: Mapping[str, str] = MappingProxyType(
    {
        "{{AI_CHAT_MIN_COINS}}": str(CHAT_MIN_TEXT_COST),
        "{{AI_CHAT_MAX_COINS}}": str(CHAT_MAX_MESSAGE_COST),
        "{{AI_CHAT_MEDIA_COINS}}": str(CHAT_MEDIA_COST),
        "{{AI_CHAT_MAX_TEXT_LENGTH}}": str(CHAT_MAX_TEXT_LENGTH),
        "{{AI_TRANSLATE_FREE_MAX_LENGTH}}": str(TRANSLATE_FREE_MAX_LENGTH),
        "{{AI_TRANSLATE_MAX_COINS}}": str(TRANSLATE_MAX_COST),
        "{{AI_TRANSLATE_MAX_TEXT_LENGTH}}": str(TRANSLATE_MAX_TEXT_LENGTH),
    }
)


def chat_text_cost(length: int) -> int:
    """文字消息按长度阶梯计费。调用方应先用 `CHAT_MAX_TEXT_LENGTH` 拒绝超长消息。"""
    for threshold, cost in CHAT_TEXT_COST_TIERS:
        if length > threshold:
            return cost
    return CHAT_MIN_TEXT_COST


def translate_text_cost(length: int) -> int:
    """`/tl` 翻译文本按长度阶梯计费。调用方应先用 `TRANSLATE_MAX_TEXT_LENGTH` 拒绝超长消息。"""
    for threshold, cost in TRANSLATE_COST_TIERS:
        if length > threshold:
            return cost
    return TRANSLATE_FREE_COST


def render_pricing_placeholders(text: str) -> str:
    """把文本里的定价占位符替换成当前声明的数字；未知占位符保持原样。"""
    rendered = text
    for placeholder, value in PRICING_PLACEHOLDERS.items():
        if placeholder in rendered:
            rendered = rendered.replace(placeholder, value)
    return rendered


__all__ = [
    "CHAT_MAX_MESSAGE_COST",
    "CHAT_MAX_TEXT_COST",
    "CHAT_MAX_TEXT_LENGTH",
    "CHAT_MEDIA_COST",
    "CHAT_MIN_TEXT_COST",
    "CHAT_TEXT_COST_TIERS",
    "PRICING_PLACEHOLDERS",
    "TRANSLATE_COST_TIERS",
    "TRANSLATE_FREE_COST",
    "TRANSLATE_FREE_MAX_LENGTH",
    "TRANSLATE_MAX_COST",
    "TRANSLATE_MAX_TEXT_LENGTH",
    "chat_text_cost",
    "render_pricing_placeholders",
    "translate_text_cost",
]
