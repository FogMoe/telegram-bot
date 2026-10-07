"""垃圾信息的累计处罚：关键词与 AI 删除的消息共用一个计数，`STRIKE_WINDOW_SECONDS` 内满 `STRIKES_TO_KICK` 次
就把发送者移出群组，`BAN_SECONDS` 内不能重新加入。

链接过滤与 @提及过滤是群规，不计入这里，它们的警告与计数仍在 `spam_control`。
计数放在进程内存里，按每次命中的时间滑动统计，重启后清零。
"""

from __future__ import annotations

import html
import logging
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from telegram.constants import ParseMode
from telegram.error import TelegramError

from fogmoe_telegram_bot.features.moderation.spam_ai import operations as spam_ai_operations

logger = logging.getLogger(__name__)

STRIKE_WINDOW_SECONDS = 3600
STRIKES_TO_KICK = 3
BAN_SECONDS = 24 * 3600
# 记录超过这个数时顺带清掉所有已过期的计数
SWEEP_THRESHOLD = 10_000
WINDOW_TEXT = f"{STRIKE_WINDOW_SECONDS // 3600} 小时内"


class StrikeTracker:
    """每个（群, 用户）在滑动窗口里的违规次数。"""

    def __init__(self, window_seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._window = window_seconds
        self._clock = clock
        self._hits: dict[tuple[int, int], deque[float]] = {}

    def _prune(self, hits: deque[float], now: float) -> None:
        while hits and now - hits[0] >= self._window:
            hits.popleft()

    def record(self, chat_id: int, user_id: int) -> int:
        """记一次违规，返回窗口内的累计次数（含这一次）。"""
        now = self._clock()
        if len(self._hits) >= SWEEP_THRESHOLD:
            for key in list(self._hits):
                self._prune(self._hits[key], now)
                if not self._hits[key]:
                    del self._hits[key]
        hits = self._hits.setdefault((chat_id, user_id), deque())
        self._prune(hits, now)
        hits.append(now)
        return len(hits)

    def clear(self, chat_id: int, user_id: int) -> None:
        self._hits.pop((chat_id, user_id), None)


TRACKER = StrikeTracker(STRIKE_WINDOW_SECONDS)


def strike_target(message: Any) -> int | None:
    """要处罚的用户；以频道或群的身份发言、或者机器人发的消息没有可以处罚的个人，返回 None。"""
    if getattr(message, "sender_chat", None) is not None:
        return None
    user = getattr(message, "from_user", None)
    if user is None or user.is_bot:
        return None
    return int(user.id)


def sender_html(message: Any) -> str:
    sender_chat = getattr(message, "sender_chat", None)
    if sender_chat is not None:
        return html.escape(sender_chat.title or "该频道")
    return message.from_user.mention_html()


async def _send(bot: Any, chat_id: int, text: str) -> None:
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
    except TelegramError as exc:
        logger.warning("发送垃圾信息处罚通知失败（群 %s）: %s", chat_id, exc)


async def penalize(bot: Any, message: Any, *, reason_html: str, note: str = "") -> None:
    """消息已经删掉之后调用：记一次违规并警告；窗口内满 `STRIKES_TO_KICK` 次时改为移出群组。

    `reason_html` 接在发送者后面，例如「发送的消息包含垃圾内容 …」；`note` 附在警告末尾。
    """
    chat_id = message.chat_id
    sender = sender_html(message)
    deleted = f"⚠️ 注意: {sender} {reason_html}，已被自动删除。"
    user_id = strike_target(message)
    if user_id is None:
        await _send(bot, chat_id, deleted + note)
        return

    count = TRACKER.record(chat_id, user_id)
    if count < STRIKES_TO_KICK:
        await _send(
            bot,
            chat_id,
            f"{deleted}\n这是第 {count} 次警告，{WINDOW_TEXT}满 {STRIKES_TO_KICK} 次将被移出群组。{note}",
        )
        return

    TRACKER.clear(chat_id, user_id)
    await kick(bot, chat_id, user_id, sender, basic_group=getattr(message.chat, "type", "") == "group")


async def kick(bot: Any, chat_id: int, user_id: int, sender: str, *, basic_group: bool = False) -> None:
    """移出群组并在 `BAN_SECONDS` 内禁止重新加入，然后在群里说明。

    普通群（非超级群）不支持限时封禁，Telegram 忽略 `until_date`，被移出的人可以凭邀请链接直接回来。
    """
    offense = f"{sender} 在 {WINDOW_TEXT}发送了 {STRIKES_TO_KICK} 次垃圾信息"
    until = datetime.now(UTC) + timedelta(seconds=BAN_SECONDS)
    try:
        await bot.ban_chat_member(chat_id=chat_id, user_id=user_id, until_date=until)
    except TelegramError as exc:
        logger.warning("移出垃圾信息发送者失败（群 %s，用户 %s）: %s", chat_id, user_id, exc)
        if "rights" in str(exc).lower():
            text = (
                f"⚠️ {offense}，但机器人没有封禁成员的权限，未能将其移出。"
                "请管理员授予机器人「封禁成员」权限。"
            )
        else:
            text = f"⚠️ {offense}，但移出失败，请管理员手动处理。"
        await _send(bot, chat_id, text)
        return

    try:
        await spam_ai_operations.forget_member(chat_id, user_id)
    except Exception:
        logger.exception("清除被移出成员的 AI 检查计数失败（群 %s，用户 %s）", chat_id, user_id)

    if basic_group:
        await _send(bot, chat_id, f"🚫 {offense}，已被移出群组。")
    else:
        hours = BAN_SECONDS // 3600
        await _send(bot, chat_id, f"🚫 {offense}，已被移出群组，{hours} 小时内无法重新加入。")
