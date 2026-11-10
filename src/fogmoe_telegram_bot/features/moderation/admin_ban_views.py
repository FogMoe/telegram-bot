"""管理员一键封禁的展示：按钮与回调数据、群里提到成员时不显示名字的写法、按完按钮后的文案。

只依赖 Telegram 的按钮类型，不发消息；按钮的处理在 `admin_ban.py`。
"""

from __future__ import annotations

from dataclasses import dataclass

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# 回调数据前缀；注册的 pattern 是 `^mod:`
CALLBACK_PREFIX = "mod"
BAN_LABEL = "🚫 封禁（管理员）"
VIEW_LABEL = "👁 原文（管理员）"

STALE_BUTTON = "这个按钮已失效。"
UNBAN_EXPIRED = "封禁已经到期，这个按钮不再有效。"
NOT_ALLOWED = "只有群主或能封禁成员的管理员可以操作。"
LOOKUP_FAILED = "无法确认你在该群的管理员权限，请稍后再试。"
BAN_FAILED = "⚠️ 封禁失败，请在群里手动处理。"
BAN_IN_PROGRESS = "正在处理，请稍候。"
UNBAN_FAILED = "⚠️ 解除封禁失败，请在群里手动处理。"
ORIGINAL_GONE = "原文已经不在了：只保存 24 小时，机器人重启后也会清空。"
ORIGINAL_EMPTY = "这条消息没有文字内容。"


def neutral_mention(user_id: int, label: str = "该用户") -> str:
    """群里提到成员时的写法：不显示名字（广告号的名字本身就是广告），点开仍能看到是谁，也会提醒对方。"""
    return f'<a href="tg://user?id={int(user_id)}">{label}</a>'


# ---------------------------------------------------------------------------
# 回调数据与按钮
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AdminAction:
    kind: str  # "ban"、"unban" 或 "view"
    chat_id: int
    user_id: int  # view：0，看原文不针对哪个人
    message_id: int | None = None  # ban：一并删除的消息；view：被删掉的那条消息
    until: int = 0  # unban：这次封禁到期的 Unix 时间，过期后按钮失效；0 表示永久封禁，按钮不过期


def ban_data(chat_id: int, user_id: int, message_id: int | None = None) -> str:
    return f"{CALLBACK_PREFIX}:ban:{chat_id}:{user_id}:{message_id or 0}"


def unban_data(chat_id: int, user_id: int, until: int = 0) -> str:
    return f"{CALLBACK_PREFIX}:unban:{chat_id}:{user_id}:{until}"


def view_data(chat_id: int, message_id: int) -> str:
    return f"{CALLBACK_PREFIX}:view:{chat_id}:{message_id}"


def parse_action(data: str | None) -> AdminAction | None:
    parts = (data or "").split(":")
    if len(parts) == 4 and parts[:2] == [CALLBACK_PREFIX, "view"]:
        try:
            return AdminAction("view", int(parts[2]), 0, message_id=int(parts[3]))
        except ValueError:
            return None
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


def view_button(chat_id: int, message_id: int) -> InlineKeyboardButton:
    return InlineKeyboardButton(VIEW_LABEL, callback_data=view_data(chat_id, message_id))


def warning_keyboard(chat_id: int, message_id: int, user_id: int | None) -> InlineKeyboardMarkup:
    """删掉消息之后的警告：看原文；能处罚到个人（`user_id` 不为 None）时再加封禁。"""
    row = [view_button(chat_id, message_id)]
    if user_id is not None:
        row.append(ban_button(chat_id, user_id))
    return InlineKeyboardMarkup([row])


def view_button_of(markup: InlineKeyboardMarkup | None) -> InlineKeyboardButton | None:
    """消息上的「原文」按钮；改写警告时留着它。"""
    for row in markup.inline_keyboard if markup is not None else ():
        for button in row:
            action = parse_action(button.callback_data)
            if action is not None and action.kind == "view":
                return button
    return None


def unban_keyboard(
    chat_id: int, user_id: int, until: int = 0, link: str | None = None, view: InlineKeyboardButton | None = None
) -> InlineKeyboardMarkup:
    """已封禁：解除封禁；消息没删掉时再给一个「查看消息」，方便手动删除；警告上原有的「原文」也留着。"""
    row = [InlineKeyboardButton("查看消息", url=link)] if link else []
    if view is not None:
        row.append(view)
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


def original_popup(text: str | None) -> str:
    """「原文」按钮的弹框：只有点按钮的管理员看得到。`text` 为 None 表示原文已经不在了。"""
    if text is None:
        return ORIGINAL_GONE
    return text or ORIGINAL_EMPTY


def admin_offense(sender_html: str) -> str:
    """从私聊里封禁时，群里移出公告的开头。"""
    return f"{sender_html} 的消息被管理员确认为垃圾信息"


def _banned(basic_group: bool) -> str:
    """普通群（非超级群）不执行封禁，被移出的人可以凭邀请链接回来，所以只说移出。"""
    return "移出群组" if basic_group else "永久封禁"


def verify_banned_text(user_id: int, admin_html: str, basic_group: bool = False) -> str:
    """验证消息上按了封禁：整条消息换成这句。"""
    return f"🚫 {neutral_mention(user_id, '新成员')} 已被 {admin_html} {_banned(basic_group)}。"


def group_banned_line(admin_html: str, basic_group: bool = False) -> str:
    """警告消息上按了封禁：接在原文后面。"""
    return f"🚫 已被 {admin_html} {_banned(basic_group)}。"


def dm_banned_line(admin_html: str, deleted: bool, basic_group: bool = False) -> str:
    if deleted:
        return f"✅ {admin_html} 已删除消息并{_banned(basic_group)}。"
    return f"⚠️ {admin_html} 已{_banned(basic_group)}，但消息没能删除，请手动删除。"


def already_out(basic_group: bool = False) -> str:
    """对方在这次操作之前就已经被封禁。普通群没有封禁名单，只能说不在群里。"""
    return "对方已经不在群里" if basic_group else "对方此前已被封禁"


def already_banned_popup(basic_group: bool = False) -> str:
    """群里的封禁按钮点在已经封禁的人身上：弹框说明，不改消息。"""
    return f"{already_out(basic_group)}，不用重复操作。"


def dm_already_banned_line(deleted: bool, basic_group: bool = False) -> str:
    """举报私聊里的封禁按钮点在已经封禁的人身上：只删被举报的消息，不再封禁和公告。"""
    if deleted:
        return f"ℹ️ {already_out(basic_group)}，没有重复处理；被举报的消息已删除。"
    return f"⚠️ {already_out(basic_group)}，但被举报的消息没能删除，请手动删除。"


def unbanned_line(admin_html: str) -> str:
    return f"✅ {admin_html} 已解除封禁，对方可以重新加入群组。"
