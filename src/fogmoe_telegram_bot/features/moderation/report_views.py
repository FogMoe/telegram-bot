"""/report 的展示：给举报人的回复、给管理员的私聊、私聊里的按钮与回调数据。

只依赖 Telegram 的按钮类型和 `operations.report_votes` 的常量，不发消息；发送与编辑由适配层（`report.py`）负责。
"""

from __future__ import annotations

import html
from dataclasses import dataclass

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from fogmoe_telegram_bot.features.moderation import admin_ban_views, spam_strikes

from .operations.report_votes import Stage, Vote

EXCERPT_CHARS = 300
BAN_HOURS = spam_strikes.BAN_SECONDS // 3600

REASON_AI_OFF = "AI 识别暂未开放"
REASON_NO_RIGHTS = "机器人没有删除消息或封禁成员的权限"
REASON_GROUP_LIMIT = "本群今天由举报触发的 AI 判断次数已用完"
REASON_TOTAL_LIMIT = "今天的 AI 判断次数已用完"
REASON_JUDGE_FAILED = "AI 判断暂时失败"

NOT_GROUP = "此命令只能在群组中使用。"
NO_REPLY = "请回复您要举报的消息，并附带 /report 命令。"
DUPLICATE = "您已经举报过这条消息了。"
SELF = "不能举报自己的消息。"
BOT = "不能举报机器人的消息。"
ADMIN = "不能举报管理员的消息。"
TOO_OLD = "只能举报 24 小时内的消息。"
LOOKUP_FAILED = "举报处理过程中出错，请稍后再试。"
AS_CHAT = "请以个人身份发送 /report；匿名管理员或以频道身份发送的举报无法计票。"

NO_ADMIN_REACHED = "无法私聊通知管理员（管理员需要先私聊过机器人），请直接联系群管理员。"


@dataclass(frozen=True, slots=True)
class ReportedMessage:
    chat_id: int
    chat_title: str
    chat_username: str | None
    message_id: int
    sender_id: int | None  # 以频道身份发的消息没有可以处罚的个人
    sender_html: str  # 已转义，可以直接放进 HTML
    text: str

    @property
    def link(self) -> str | None:
        """被举报消息的链接；私有的普通群没有可用的链接。"""
        if self.chat_username:
            return f"https://t.me/{self.chat_username}/{self.message_id}"
        chat = str(self.chat_id)
        if chat.startswith("-100"):
            return f"https://t.me/c/{chat[4:]}/{self.message_id}"
        return None


# ---------------------------------------------------------------------------
# 按钮（回调由 `admin_ban.py` 处理）
# ---------------------------------------------------------------------------


def pending_keyboard(reported: ReportedMessage) -> InlineKeyboardMarkup | None:
    """还没处理的举报：查看消息、一键删除并永久封禁。"""
    row = []
    if reported.link:
        row.append(InlineKeyboardButton("查看消息", url=reported.link))
    if reported.sender_id is not None:
        row.append(
            admin_ban_views.ban_button(reported.chat_id, reported.sender_id, reported.message_id, "删除并永久封禁")
        )
    return InlineKeyboardMarkup([row]) if row else None


def link_keyboard(reported: ReportedMessage) -> InlineKeyboardMarkup | None:
    """只有「查看消息」：对方已经被封禁、消息还没删掉时用。"""
    if not reported.link:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton("查看消息", url=reported.link)]])


def banned_keyboard(chat_id: int, user_id: int, until: int, link: str | None = None) -> InlineKeyboardMarkup:
    """自动封禁之后：解除封禁（到期后失效）；消息没删掉时再给一个「查看消息」。"""
    return admin_ban_views.unban_keyboard(chat_id, user_id, until, link)


# ---------------------------------------------------------------------------
# 给管理员的私聊
# ---------------------------------------------------------------------------


def _card(reported: ReportedMessage, count: int) -> str:
    text = reported.text or "（没有文字内容）"
    excerpt = text if len(text) <= EXCERPT_CHARS else text[:EXCERPT_CHARS] + "…"
    sender_id = f"（ID <code>{reported.sender_id}</code>）" if reported.sender_id is not None else ""
    return (
        f"<b>群组：</b>{html.escape(reported.chat_title)}\n"
        f"<b>被举报用户：</b>{reported.sender_html}{sender_id}\n"
        f"<b>举报人数：</b>{count}\n\n"
        f"<b>被举报消息：</b>\n{html.escape(excerpt)}"
    )


def first_report_dm(reported: ReportedMessage, reporter_html: str, required: int | None) -> str:
    """第一次举报。`required` 为 None 时这条消息不会自动处理（以频道身份发送）。"""
    if required is None:
        footer = "这条消息是以频道身份发送的，不会自动处理。"
    else:
        footer = (
            f"凑够 {required} 人举报后会交给 AI 判断，确认是垃圾信息会自动删除并封禁 {BAN_HOURS} 小时，"
            "届时会再通知你。"
        )
    return f"📣 <b>新举报</b>\n\n{_card(reported, 1)}\n\n<b>举报人：</b>{reporter_html}\n\n{footer}"


def escalated_dm(reported: ReportedMessage, count: int, reason: str) -> str:
    return f"🔎 <b>举报需要你处理</b>：举报人数已够，但{reason}。\n\n{_card(reported, count)}"


def not_spam_dm(reported: ReportedMessage, count: int, probability: float) -> str:
    return (
        f"🔎 <b>举报需要你处理</b>：AI 认为这条消息不是垃圾信息（概率 {probability:.0%}），没有自动处理。\n\n"
        f"{_card(reported, count)}"
    )


def auto_banned_dm(reported: ReportedMessage, count: int, probability: float, *, banned: bool, deleted: bool) -> str:
    verdict = f"AI 判定为垃圾信息（概率 {probability:.0%}）"
    if banned and deleted:
        head = (
            f"🚫 <b>举报已自动处理</b>：{verdict}，已删除消息并封禁 {BAN_HOURS} 小时。"
            "误判可以点下面的按钮解除封禁。"
        )
    elif banned:
        head = f"⚠️ <b>举报需要你处理</b>：{verdict}，已封禁 {BAN_HOURS} 小时，但消息没能删除，请手动删除。"
    elif deleted:
        head = f"⚠️ <b>举报需要你处理</b>：{verdict}，已删除消息，但没能封禁，请在群里手动处理。"
    else:
        head = f"⚠️ <b>举报需要你处理</b>：{verdict}，但消息没能删除，也没能封禁，请在群里手动处理。"
    return f"{head}\n\n{_card(reported, count)}"


def already_banned_dm(
    reported: ReportedMessage, count: int, probability: float, *, deleted: bool, basic_group: bool = False
) -> str:
    """AI 判定为垃圾信息时对方已经被封禁（管理员先处理了）：只删了消息。"""
    verdict = f"AI 判定为垃圾信息（概率 {probability:.0%}）"
    out = admin_ban_views.already_out(basic_group)
    if deleted:
        head = f"🚫 <b>举报已自动处理</b>：{verdict}，已删除消息。{out}，没有重复处理。"
    else:
        head = f"⚠️ <b>举报需要你处理</b>：{verdict}，{out}，但消息没能删除，请手动删除。"
    return f"{head}\n\n{_card(reported, count)}"


# ---------------------------------------------------------------------------
# 群里的回复与公告
# ---------------------------------------------------------------------------


def vote_offense(sender_html: str, count: int) -> str:
    """凑够票数、AI 确认后的移出公告开头。"""
    return f"{sender_html} 的消息经 {count} 人举报、AI 确认为垃圾信息"


def _admins_note(notified: int | None) -> str:
    if notified is None:
        return ""
    return "已通知管理员。" if notified else NO_ADMIN_REACHED


def vote_reply(vote: Vote, required: int, notified: int | None) -> str:
    """这次举报没有触发判断时的回复。`notified` 是这次私聊到的管理员人数，没有私聊时为 None。"""
    if vote.stage is Stage.JUDGING:
        return f"已收到举报（{vote.count} 人），AI 正在判断这条消息。"
    if vote.stage is Stage.DONE:
        return f"已收到举报（{vote.count} 人），这条消息已经处理过了。"
    text = f"已收到举报（{vote.count}/{required}）。{_admins_note(notified)}"
    if vote.count < required:
        text += f"凑够 {required} 人举报后由 AI 判断，确认是垃圾信息会自动删除并封禁。"
    return text


def channel_reply(first: bool, notified: int | None) -> str:
    if not first:
        return "已收到举报。这条消息是以频道身份发送的，不会自动处理，需要管理员处理。"
    return f"已收到举报。{_admins_note(notified)}这条消息是以频道身份发送的，不会自动处理。"


def reporter_limit_reply(count: int, required: int, notified: int | None) -> str:
    return (
        f"已收到举报（{count}/{required}）。{_admins_note(notified)}"
        "你今天触发 AI 判断的次数已用完，需要其他成员举报或管理员处理。"
    )


def escalated_reply(count: int, required: int, reason: str, notified: int) -> str:
    handed = "已交给管理员处理。" if notified else NO_ADMIN_REACHED
    return f"已收到举报（{count}/{required}）。{reason}，{handed}"


def already_banned_reply(count: int, required: int, *, deleted: bool, basic_group: bool = False) -> str:
    done = "消息已删除" if deleted else "消息没能删除，已通知管理员"
    return (
        f"已收到举报（{count}/{required}）。AI 确认为垃圾信息，"
        f"{admin_ban_views.already_out(basic_group)}，{done}。"
    )


def not_spam_reply(count: int, required: int, notified: int) -> str:
    handed = "已交给管理员处理。" if notified else NO_ADMIN_REACHED
    return f"已收到举报（{count}/{required}）。AI 认为这条消息不是垃圾信息，{handed}"
