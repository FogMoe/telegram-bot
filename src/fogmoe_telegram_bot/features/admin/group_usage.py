"""`/stats` 与 `/admin_announce` 读取「哪些群在用群管功能」的入口。

关键词回复、入群验证与垃圾信息过滤的表属于 `features/moderation`，功能之间不互相 import，
所以由 app 在注册 admin 的 handler 时注入 moderation 的读取函数（`install`）。没有注入时按没有群处理。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

# 读取一类群：(群数, 前 limit 个群 id)；limit 为 None 时列出全部
GroupReader = Callable[[int | None], Awaitable[tuple[int, list[int]]]]


async def _no_groups(limit: int | None = None) -> tuple[int, list[int]]:
    return 0, []


@dataclass(frozen=True, slots=True)
class ModerationGroups:
    keywords: GroupReader  # 设置了关键词回复的群
    verification: GroupReader  # 开启了入群验证的群
    spam_filter: GroupReader  # 垃圾信息过滤总开关打开的群
    spam_settings: GroupReader  # 设置过垃圾信息过滤的群，不论总开关


NOT_INSTALLED = ModerationGroups(_no_groups, _no_groups, _no_groups, _no_groups)
_moderation = NOT_INSTALLED


def install(groups: ModerationGroups) -> None:
    global _moderation
    _moderation = groups


def moderation_groups() -> ModerationGroups:
    return _moderation
