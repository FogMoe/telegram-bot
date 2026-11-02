"""管理员一键封禁的展示：按钮与回调数据、群里提到成员时不显示名字的写法、按完按钮后的文案。

只依赖 Telegram 的按钮类型，不发消息；按钮的处理在 `admin_ban.py`。
"""

from __future__ import annotations

from dataclasses import dataclass

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# 回调数据前缀；注册的 pattern 是 `^mod:`
CALLBACK_PREFIX = "mod"
BAN_LABEL = "🚫 封禁（管理员）"

STALE_BUTTON = "这个按钮已失效。"
UNBAN_EXPIRED = "封禁已经到期，这个按钮不再有效。"
NOT_ALLOWED = "只有群主或能封禁成员的管理员可以操作。"
LOOKUP_FAILED = "无法确认你在该群的管理员权限，请稍后再试。"
BAN_FAILED = "⚠️ 封禁失败，请在群里手动处理。"
UNBAN_FAILED = "⚠️ 解除封禁失败，请在群里手动处理。"


def neutral_mention(user_id: int, label: str = "该用户") -> str:
    """群里提到成员时的写法：不显示名字（广告号的名字本身就是广告），点开仍能看到是谁，也会提醒对方。"""
    return f'<a href="tg://user?id={int(user_id)}">{label}</a>'


# ---------------------------------------------------------------------------
# 回调数据与按钮
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AdminAction:
    kind: str  # "ban" 或 "unban"
    chat_id: int
    user_id: int
    message_id: int | None = None  # ban：一并删除的消息
    until: int = 0  # unban：这次封禁到期的 Unix 时间，过期后按钮失效；0 表示永久封禁，按钮不过期


def ban_data(chat_id: int, user_id: int, message_id: int | None = None) -> str:
    return f"{CALLBACK_PREFIX}:ban:{chat_id}:{user_id}:{message_id or 0}"


def unban_data(chat_id: int, user_id: int, until: int = 0) -> str:
    return f"{CALLBACK_PREFIX}:unban:{chat_id}:{user_id}:{until}"


def parse_action(data: str | None) -> AdminAction | None:
    parts = (data or "").split(":")
    if len(parts) != 5 or parts[0] != CALLBACK_PREFIX or parts[1] not in ("ban", "unban"):
        return None
    try:
        chat_id, user_id, extra = int(parts[2]), int(parts[3]), int(parts[4])
    except ValueError:
        return None
    if parts[1] == "ban":
        return AdminAction("ban", chat_id, user_id, message_id=extra or None)
    return AdminAction("unban", chat_id, user_id, until=extra)


def ban_button(chat_id: int, user_id: int, message_id: int | None = None, label: str = BAN_LABEL) -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data=ban_data(chat_id, user_id, message_id))


def ban_keyboard(chat_id: int, user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[ban_button(chat_id, user_id)]])


def unban_keyboard(chat_id: int, user_id: int, until: int = 0, link: str | None = None) -> InlineKeyboardMarkup:
    """已封禁：解除封禁；消息没删掉时再给一个「查看消息」，方便手动删除。"""
    row = [InlineKeyboardButton("查看消息", url=link)] if link else []
    row.append(InlineKeyboardButton("解除封禁", callback_data=unban_data(chat_id, user_id, until)))
    return InlineKeyboardMarkup([row])


def offered(markup: InlineKeyboardMarkup | None, data: str | None) -> bool:
    """回调数据确实是这条消息上的按钮：只接受机器人自己发出的按钮，不接受伪造的回调。"""
    if markup is None or data is None:
        return False
    return any(button.callback_data == data for row in markup.inline_keyboard for button in row)


def without_button(markup: InlineKeyboardMarkup | None, data: str) -> InlineKeyboardMarkup | None:
    """去掉回调数据为 `data` 的按钮，其余按钮（例如「查看消息」）保留；一个不剩时返回 None。"""
    if markup is None:
        return None
    rows = [[button for button in row if button.callback_data != data] for row in markup.inline_keyboard]
    rows = [row for row in rows if row]
    return InlineKeyboardMarkup(rows) if rows else None


# ---------------------------------------------------------------------------
# 按完按钮之后
# ---------------------------------------------------------------------------


def admin_offense(sender_html: str) -> str:
    """从私聊里封禁时，群里移出公告的开头。"""
    return f"{sender_html} 的消息被管理员确认为垃圾信息"


def verify_banned_text(user_id: int, admin_html: str) -> str:
    """验证消息上按了封禁：整条消息换成这句。"""
    return f"🚫 {neutral_mention(user_id, '新成员')} 已被 {admin_html} 永久封禁。"


def group_banned_line(admin_html: str) -> str:
    """警告消息上按了封禁：接在原文后面。"""
    return f"🚫 已被 {admin_html} 永久封禁。"


def dm_banned_line(admin_html: str, deleted: bool) -> str:
    if deleted:
        return f"✅ {admin_html} 已删除消息并永久封禁。"
    return f"⚠️ {admin_html} 已永久封禁，但消息没能删除，请手动删除。"


def unbanned_line(admin_html: str) -> str:
    return f"✅ {admin_html} 已解除封禁，对方可以重新加入群组。"
