"""/report 的规则：每条消息的举报票数、凑够票数后交给 AI 判断，以及防滥用的每日额度。不依赖 Telegram。

规则（已确认的产品决定）：

- 每 `MEMBERS_PER_VOTE` 名成员需要 1 票，最少 1 票、最多 `MAX_VOTES` 票；同一人对同一条消息只算一票。
- 凑够票数后每条消息只交给 AI 判断一次；之后的举报只计票。
- 所有群都能用。已开通 /spam ai 的群和 AI 识别共用每天的调用额度；其余群每个每天最多
  `FREE_GROUP_DAILY_CHECKS` 次，合计每天最多 `FREE_TOTAL_DAILY_CHECKS` 次；
  每位举报人每天最多触发 `REPORTER_DAILY_CHECKS` 次（UTC 日期）。
- 只能举报 `REPORTABLE_SECONDS` 内发出的消息。

状态都放在进程内存里，重启后清零。
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum

from fogmoe_telegram_bot.features.moderation.spam_ai import operations as spam_ai_operations

MEMBERS_PER_VOTE = 100
MAX_VOTES = 5
REPORTABLE_SECONDS = 24 * 3600
FREE_GROUP_DAILY_CHECKS = 20
FREE_TOTAL_DAILY_CHECKS = 2000
REPORTER_DAILY_CHECKS = 10
# 举报记录保留这么久：被举报的消息最多 24 小时前发出，机器人 48 小时后就删不了消息
RECORD_SECONDS = 48 * 3600


def required_votes(member_count: int | None) -> int:
    """触发 AI 判断需要的票数；查不到成员数时按最多的票数算。"""
    if member_count is None:
        return MAX_VOTES
    return min(MAX_VOTES, max(1, math.ceil(member_count / MEMBERS_PER_VOTE)))


def too_old(sent_at: datetime, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    return now - sent_at > timedelta(seconds=REPORTABLE_SECONDS)


class Stage(StrEnum):
    COLLECTING = "collecting"  # 还没交给 AI
    JUDGING = "judging"  # 有一次举报正在处理判断
    DONE = "done"  # 已经判断过，或者已经转交管理员


class VoteStatus(StrEnum):
    COUNTED = "counted"
    DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class Vote:
    status: VoteStatus
    count: int
    first: bool  # 这条消息的第一次举报
    stage: Stage


@dataclass(slots=True)
class _Reports:
    first_at: float
    reporters: set[int] = field(default_factory=set)
    stage: Stage = Stage.COLLECTING


class ReportBook:
    """每条被举报的消息：谁举报过、处理到哪一步。"""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._reports: dict[tuple[int, int], _Reports] = {}

    def _sweep(self, now: float) -> None:
        for key in [key for key, item in self._reports.items() if now - item.first_at >= RECORD_SECONDS]:
            del self._reports[key]

    def add(self, chat_id: int, message_id: int, reporter_id: int) -> Vote:
        now = self._clock()
        self._sweep(now)
        item = self._reports.get((chat_id, message_id))
        first = item is None
        if item is None:
            item = self._reports[(chat_id, message_id)] = _Reports(first_at=now)
        if reporter_id in item.reporters:
            return Vote(VoteStatus.DUPLICATE, len(item.reporters), False, item.stage)
        item.reporters.add(reporter_id)
        return Vote(VoteStatus.COUNTED, len(item.reporters), first, item.stage)

    def claim(self, chat_id: int, message_id: int) -> bool:
        """由这次举报负责判断：还在收集票数时转入 JUDGING 并返回 True，别的举报已经接手时返回 False。"""
        item = self._reports.get((chat_id, message_id))
        if item is None or item.stage is not Stage.COLLECTING:
            return False
        item.stage = Stage.JUDGING
        return True

    def release(self, chat_id: int, message_id: int) -> None:
        """这次举报没能判断（举报人额度用完），交还给后面的举报。"""
        item = self._reports.get((chat_id, message_id))
        if item is not None and item.stage is Stage.JUDGING:
            item.stage = Stage.COLLECTING

    def finish(self, chat_id: int, message_id: int) -> None:
        item = self._reports.get((chat_id, message_id))
        if item is not None:
            item.stage = Stage.DONE


BOOK = ReportBook()


class Quota(StrEnum):
    OK = "ok"
    REPORTER_LIMIT = "reporter_limit"
    GROUP_LIMIT = "group_limit"
    TOTAL_LIMIT = "total_limit"


def _today() -> date:
    return datetime.now(UTC).date()


class DailyQuota:
    """举报触发的 AI 判断的每日额度。"""

    def __init__(self) -> None:
        self._day: date | None = None
        self._groups: dict[int, int] = {}
        self._reporters: dict[int, int] = {}
        self._total = 0

    def _roll(self, today: date) -> None:
        if self._day != today:
            self._day = today
            self._groups.clear()
            self._reporters.clear()
            self._total = 0

    def take(self, chat_id: int, reporter_id: int, *, paid: bool, today: date | None = None) -> Quota:
        """额度够就记一次并返回 OK；不够时什么也不记。付费群的群额度由 AI 识别那边扣。"""
        today = today or _today()
        self._roll(today)
        if self._reporters.get(reporter_id, 0) >= REPORTER_DAILY_CHECKS:
            return Quota.REPORTER_LIMIT
        if paid:
            if not spam_ai_operations.take_daily_check(chat_id, today):
                return Quota.GROUP_LIMIT
        else:
            if self._groups.get(chat_id, 0) >= FREE_GROUP_DAILY_CHECKS:
                return Quota.GROUP_LIMIT
            if self._total >= FREE_TOTAL_DAILY_CHECKS:
                return Quota.TOTAL_LIMIT
            self._groups[chat_id] = self._groups.get(chat_id, 0) + 1
            self._total += 1
        self._reporters[reporter_id] = self._reporters.get(reporter_id, 0) + 1
        return Quota.OK


QUOTA = DailyQuota()
