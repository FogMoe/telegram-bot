"""新成员的加群系统消息（「X 加入了群组」）：记下消息 id，验证没通过或被封禁时删掉，免得广告名字一直挂在群里。

不依赖 Telegram。只放在进程内存里，保留 `KEEP_SECONDS`（机器人 48 小时后就删不了消息），重启后清零。
"""

from __future__ import annotations

import time
from collections.abc import Callable

KEEP_SECONDS = 48 * 3600
# 记录超过这个数时先清掉过期的，还是太多就整体清空
MAX_ENTRIES = 50_000


class JoinMessages:
    """每个（群, 用户）最近一次加群的系统消息 id。"""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._items: dict[tuple[int, int], tuple[float, int]] = {}

    def record(self, chat_id: int, user_id: int, message_id: int) -> None:
        now = self._clock()
        if len(self._items) >= MAX_ENTRIES:
            self._items = {key: item for key, item in self._items.items() if now - item[0] < KEEP_SECONDS}
            if len(self._items) >= MAX_ENTRIES:
                self._items.clear()
        self._items[(chat_id, user_id)] = (now, message_id)

    def pop(self, chat_id: int, user_id: int) -> int | None:
        """取出并忘掉这条消息的 id；没有记录或已经过期时返回 None。"""
        item = self._items.pop((chat_id, user_id), None)
        if item is None or self._clock() - item[0] >= KEEP_SECONDS:
            return None
        return item[1]


JOINS = JoinMessages()
