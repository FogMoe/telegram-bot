"""哪些群在用群管功能：给管理员的 `/stats` 与 `/admin_announce` 读取。不依赖 Telegram。

admin 不 import 本功能，这些函数由 app 在启动时注入 `features/admin/group_usage.py`。
`limit` 为 None 时列出全部群，否则只列前 `limit` 个（不排序）；`total` 总是全部的群数。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import NamedTuple

from ..repositories import keyword_replies, spam_settings, verification


class GroupUsage(NamedTuple):
    total: int  # 全部的群数
    group_ids: list[int]


async def _usage(
    count: Callable[[], Awaitable[int]],
    list_ids: Callable[[int | None], Awaitable[list[int]]],
    limit: int | None,
) -> GroupUsage:
    group_ids = await list_ids(limit)
    return GroupUsage(len(group_ids) if limit is None else await count(), group_ids)


async def keyword_groups(limit: int | None = None) -> GroupUsage:
    """设置了关键词回复的群。"""
    return await _usage(keyword_replies.count_groups, keyword_replies.list_group_ids, limit)


async def verification_groups(limit: int | None = None) -> GroupUsage:
    """开启了入群验证的群。"""
    return await _usage(verification.count_groups, verification.list_group_ids, limit)


async def spam_filter_groups(limit: int | None = None) -> GroupUsage:
    """垃圾信息过滤总开关打开的群。"""

    async def count() -> int:
        return await spam_settings.count_groups(enabled_only=True)

    async def list_ids(limit: int | None) -> list[int]:
        return await spam_settings.list_group_ids(limit, enabled_only=True)

    return await _usage(count, list_ids, limit)


async def spam_settings_groups(limit: int | None = None) -> GroupUsage:
    """设置过垃圾信息过滤的群，不论总开关是否打开。"""

    async def count() -> int:
        return await spam_settings.count_groups(enabled_only=False)

    async def list_ids(limit: int | None) -> list[int]:
        return await spam_settings.list_group_ids(limit, enabled_only=False)

    return await _usage(count, list_ids, limit)
