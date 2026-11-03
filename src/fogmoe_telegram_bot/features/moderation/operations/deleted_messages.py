"""被自动删除的消息的原文：警告下面的「原文（管理员）」按钮用弹框给管理员看，群里其他人看不到。

不依赖 Telegram。只放在进程内存里，保留 `KEEP_SECONDS`，重启后清零；只存弹框放得下的长度。
"""

from __future__ import annotations

import time
from collections.abc import Callable

KEEP_SECONDS = 24 * 3600
# 记录满这个数时丢掉最早记下的，其余的照常保留到期
MAX_ENTRIES = 20_000
# Telegram 回调弹框最多 200 个字符（按 UTF-16 计）
POPUP_LIMIT = 200


def _units(char: str) -> int:
    return 2 if ord(char) > 0xFFFF else 1


def clip(text: str, limit: int = POPUP_LIMIT) -> str:
    """截到 `limit` 个 UTF-16 单位以内，截掉了就以「…」结尾。"""
    if sum(_units(char) for char in text) <= limit:
        return text
    kept, used = [], 1  # 留一个位置给「…」
    for char in text:
        used += _units(char)
        if used > limit:
            break
        kept.append(char)
    return "".join(kept) + "…"


class DeletedMessages:
    """每个（群, 被删消息 id）的原文。"""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._items: dict[tuple[int, int], tuple[float, str]] = {}

    def record(self, chat_id: int, message_id: int, text: str) -> None:
        now = self._clock()
        key = (chat_id, message_id)
        # 重新记下的排到最后，字典的顺序就是记录时间的顺序：从头清掉过期的，满了再丢最早的
        self._items.pop(key, None)
        while self._items:
            oldest = next(iter(self._items))
            if len(self._items) < MAX_ENTRIES and now - self._items[oldest][0] < KEEP_SECONDS:
                break
            del self._items[oldest]
        self._items[key] = (now, clip(text))

    def get(self, chat_id: int, message_id: int) -> str | None:
        """原文（可以反复查看）；没有记录或已经过期时返回 None。"""
        item = self._items.get((chat_id, message_id))
        if item is None or self._clock() - item[0] >= KEEP_SECONDS:
            return None
        return item[1]


ORIGINALS = DeletedMessages()
