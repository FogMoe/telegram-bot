"""垃圾信息的累计处罚：关键词与 AI 删除的消息共用一个计数，`STRIKE_WINDOW_SECONDS` 内满 `STRIKES_TO_KICK` 次
就把发送者移出群组，`BAN_SECONDS` 内不能重新加入。

链接过滤、@提及过滤与全局垃圾词库的命中不计入这里，它们只警告，警告次数在 `operations/spam_control.py`。
计数放在进程内存里，按每次命中的时间滑动统计，重启后清零。

`ban` 与 `kick` 也给举报和管理员按钮用：封禁时一并删掉对方的加群消息。群里的警告与公告不显示对方的名字。
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from telegram.constants import ParseMode
from telegram.error import TelegramError

from fogmoe_telegram_bot.features.moderation import admin_ban_views
from fogmoe_telegram_bot.features.moderation.operations import join_messages
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


def sender_user(message: Any) -> Any:
    """消息算在谁头上。访客 bot（Bot API 10.0 的 Guest Mode：不在群里，被人 @ 一次就能回复一次）的回复
    算在 @ 它的人头上，其余是发送者本人。"""
    return getattr(message, "guest_bot_caller_user", None) or getattr(message, "from_user", None)


def sender_chat(message: Any) -> Any:
    """以频道或群的身份发言时的那个频道或群；访客 bot 的回复看 @ 它的是不是频道或群。"""
    return getattr(message, "sender_chat", None) or getattr(message, "guest_bot_caller_chat", None)


def strike_target(message: Any) -> int | None:
    """要处罚的用户；以频道或群的身份发言、或者机器人自己发的消息没有可以处罚的个人，返回 None。"""
    if sender_chat(message) is not None:
        return None
    user = sender_user(message)
    if user is None or user.is_bot:
        return None
    return int(user.id)


def sender_html(message: Any) -> str:
    """群里提到发送者时的写法：不显示名字或频道名（广告号的名字本身就是广告），点开仍能看到是谁。"""
    if sender_chat(message) is not None:
        return "该频道"
    return admin_ban_views.neutral_mention(sender_user(message).id)


async def _send(bot: Any, chat_id: int, text: str, reply_markup: Any = None) -> None:
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except TelegramError as exc:
        logger.warning("发送垃圾信息处罚通知失败（群 %s）: %s", chat_id, exc)


async def penalize(bot: Any, message: Any, *, reason_html: str, note: str = "") -> None:
    """消息已经删掉之后调用：记一次违规并警告；窗口内满 `STRIKES_TO_KICK` 次时改为移出群组。

    `reason_html` 接在发送者后面，例如「发送的消息包含垃圾内容 …」；`note` 附在警告末尾。
    警告下面带管理员的封禁按钮。
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
            admin_ban_views.ban_keyboard(chat_id, user_id),
        )
        return

    TRACKER.clear(chat_id, user_id)
    await kick(bot, chat_id, user_id, sender, basic_group=getattr(message.chat, "type", "") == "group")


async def delete_join_message(bot: Any, chat_id: int, user_id: int) -> None:
    """删掉这个人的加群系统消息（记得的话）。"""
    message_id = join_messages.JOINS.pop(chat_id, user_id)
    if message_id is None:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramError as exc:
        logger.info("删除加群消息失败（群 %s，消息 %s）: %s", chat_id, message_id, exc)


async def ban(bot: Any, chat_id: int, user_id: int, *, seconds: int | None = BAN_SECONDS) -> TelegramError | None:
    """封禁并清理（AI 检查计数、加群消息）；`seconds` 为 None 时永久封禁。成功返回 None，失败返回 Telegram 的错误。"""
    until = None if seconds is None else datetime.now(UTC) + timedelta(seconds=seconds)
    try:
        await bot.ban_chat_member(chat_id=chat_id, user_id=user_id, until_date=until)
    except TelegramError as exc:
        logger.warning("封禁失败（群 %s，用户 %s）: %s", chat_id, user_id, exc)
        return exc

    try:
        await spam_ai_operations.forget_member(chat_id, user_id)
    except Exception:
        logger.exception("清除被移出成员的 AI 检查计数失败（群 %s，用户 %s）", chat_id, user_id)
    await delete_join_message(bot, chat_id, user_id)
    return None


async def kick(
    bot: Any,
    chat_id: int,
    user_id: int,
    sender: str,
    *,
    basic_group: bool = False,
    offense: str | None = None,
    seconds: int | None = BAN_SECONDS,
) -> bool:
    """移出群组并封禁 `seconds` 秒（None 为永久），然后在群里说明；移出成功返回 True。

    `offense` 是群里说明的开头，默认是累计处罚的说法。
    普通群（非超级群）不支持限时封禁，Telegram 忽略 `until_date`，被移出的人可以凭邀请链接直接回来。
    """
    offense = offense or f"{sender} 在 {WINDOW_TEXT}发送了 {STRIKES_TO_KICK} 次垃圾信息"
    error = await ban(bot, chat_id, user_id, seconds=seconds)
    if error is not None:
        if "rights" in str(error).lower():
            text = (
                f"⚠️ {offense}，但机器人没有封禁成员的权限，未能将其移出。"
                "请管理员授予机器人「封禁成员」权限。"
            )
        else:
            text = f"⚠️ {offense}，但移出失败，请管理员手动处理。"
        await _send(bot, chat_id, text)
        return False

    if basic_group:
        await _send(bot, chat_id, f"🚫 {offense}，已被移出群组。")
    elif seconds is None:
        await _send(bot, chat_id, f"🚫 {offense}，已被移出群组并永久封禁。")
    else:
        await _send(bot, chat_id, f"🚫 {offense}，已被移出群组，{seconds // 3600} 小时内无法重新加入。")
    return True
