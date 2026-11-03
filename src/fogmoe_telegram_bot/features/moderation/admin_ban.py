"""管理员一键封禁：验证消息、垃圾信息警告和举报私聊上的「封禁」「解除封禁」按钮，以及警告上的「原文」按钮。

只有群主和能封禁成员的管理员能点。管理员手动封禁是永久的（自动处罚是 24 小时），封禁时一并删掉对方的加群消息；
从举报私聊里封禁还会删掉被举报的消息，并在群里发移出公告。在群里的按钮上封禁时，改写那条消息本身，不另发公告。
「原文」用回调弹框显示被自动删除的消息，只有点的人看得到，也不记进对方的 AI 对话记录；原文在 `operations/deleted_messages.py`。

按钮与文案在 `admin_ban_views.py`，封禁本身在 `spam_strikes.ban` / `spam_strikes.kick`。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from telegram import InlineKeyboardMarkup, LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import CallbackQueryHandler, ContextTypes

from fogmoe_telegram_bot.core.telegram_history import suppress_telegram_history
from fogmoe_telegram_bot.features.moderation import admin_ban_views as views
from fogmoe_telegram_bot.features.moderation import spam_strikes
from fogmoe_telegram_bot.features.moderation.admin_ban_views import AdminAction
from fogmoe_telegram_bot.features.moderation.operations import deleted_messages, verify_game
from fogmoe_telegram_bot.features.moderation.operations import member_verify as verification

logger = logging.getLogger(__name__)

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


def is_basic_group(chat_id: int) -> bool:
    """普通群（非超级群）；超级群的 chat id 以 -100 开头。"""
    return not str(chat_id).startswith("-100")


def can_ban(member: Any) -> bool:
    """点按钮的人是群主，或者是能封禁成员的管理员。"""
    if member.status == "creator":
        return True
    return member.status == "administrator" and bool(getattr(member, "can_restrict_members", False))


async def delete_message(bot: Any, chat_id: int, message_id: int) -> bool:
    """删除消息；消息已经不在时也算删掉。"""
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramError as exc:
        if "not found" in str(exc).lower():
            return True
        logger.warning("删除群 %s 的消息 %s 失败: %s", chat_id, message_id, exc)
        return False
    return True


async def _ban_from_dm(bot: Any, action: AdminAction, admin_html: str, text_html: str) -> tuple[str, InlineKeyboardMarkup] | None:
    """举报私聊里的封禁：删消息、永久封禁、群里发公告。失败返回 None。"""
    deleted = action.message_id is None or await delete_message(bot, action.chat_id, action.message_id)
    sender = views.neutral_mention(action.user_id)
    banned = await spam_strikes.kick(
        bot,
        action.chat_id,
        action.user_id,
        sender,
        basic_group=is_basic_group(action.chat_id),
        offense=views.admin_offense(sender),
        seconds=None,
    )
    if not banned:
        return None
    return (
        f"{text_html}\n\n{views.dm_banned_line(admin_html, deleted, is_basic_group(action.chat_id))}",
        views.unban_keyboard(action.chat_id, action.user_id),
    )


async def _ban_in_group(
    bot: Any, action: AdminAction, admin_html: str, message: Any
) -> tuple[str, InlineKeyboardMarkup] | None:
    """群里验证消息或警告上的封禁：永久封禁，改写这条消息。失败返回 None。"""
    if await spam_strikes.ban(bot, action.chat_id, action.user_id, seconds=None) is not None:
        return None
    keyboard = views.unban_keyboard(action.chat_id, action.user_id, view=views.view_button_of(message.reply_markup))
    basic_group = is_basic_group(action.chat_id)
    message_id, text_html = message.message_id, message.text_html
    # 验证消息：结束这一轮验证，免得到点又处理一次；整条消息换成封禁结果
    if await verification.find_round_message(action.user_id, action.chat_id) == message_id:
        await verification.claim_round(action.user_id, action.chat_id, message_id)
        verify_game.GAMES.drop(action.chat_id, action.user_id, message_id)
        return views.verify_banned_text(action.user_id, admin_html, basic_group), keyboard
    return f"{text_html}\n\n{views.group_banned_line(admin_html, basic_group)}", keyboard


async def _unban(bot: Any, action: AdminAction, admin_html: str, message: Any) -> tuple[str, InlineKeyboardMarkup | None] | None:
    """解除封禁：去掉按钮，警告上原有的「原文」留着。失败返回 None。"""
    try:
        await bot.unban_chat_member(chat_id=action.chat_id, user_id=action.user_id, only_if_banned=True)
    except TelegramError as exc:
        logger.warning("解除群 %s 用户 %s 的封禁失败: %s", action.chat_id, action.user_id, exc)
        return None
    view = views.view_button_of(message.reply_markup)
    return f"{message.text_html}\n\n{views.unbanned_line(admin_html)}", InlineKeyboardMarkup([[view]]) if view else None


async def admin_ban_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    message = query.message
    action = views.parse_action(query.data)
    if action is None or not views.offered(getattr(message, "reply_markup", None), query.data):
        await query.answer(views.STALE_BUTTON, show_alert=True)
        return
    if action.kind == "unban" and action.until and time.time() >= action.until:
        await query.answer(views.UNBAN_EXPIRED, show_alert=True)
        try:
            # 只去掉过期的解除封禁按钮；消息没删掉时的「查看消息」还要留着
            await query.edit_message_reply_markup(reply_markup=views.without_button(message.reply_markup, query.data))
        except TelegramError as exc:
            logger.info("移除过期的解除封禁按钮失败: %s", exc)
        return
    try:
        member = await context.bot.get_chat_member(action.chat_id, query.from_user.id)
    except TelegramError as exc:
        logger.warning("确认群 %s 的管理员 %s 失败: %s", action.chat_id, query.from_user.id, exc)
        await query.answer(views.LOOKUP_FAILED, show_alert=True)
        return
    if not can_ban(member):
        await query.answer(views.NOT_ALLOWED, show_alert=True)
        return
    if action.kind == "view":
        original = deleted_messages.ORIGINALS.get(action.chat_id, action.message_id)
        # 弹框默认会记进点按钮的人的 AI 对话记录；原文只给管理员看一眼，不能借此存进数据库
        with suppress_telegram_history():
            await query.answer(views.original_popup(original), show_alert=True)
        return

    admin_html = query.from_user.mention_html()
    if action.kind == "unban":
        result = await _unban(context.bot, action, admin_html, message)
        failure = views.UNBAN_FAILED
    elif message.chat.type == "private":
        result = await _ban_from_dm(context.bot, action, admin_html, message.text_html)
        failure = views.BAN_FAILED
    else:
        result = await _ban_in_group(context.bot, action, admin_html, message)
        failure = views.BAN_FAILED
    if result is None:
        await query.answer(failure, show_alert=True)  # 保留原来的按钮，可以再试
        return

    await query.answer()
    text, keyboard = result
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard, link_preview_options=NO_PREVIEW)
    except TelegramError as exc:
        logger.warning("更新封禁按钮所在的消息失败: %s", exc)


def setup_admin_ban_handlers(application) -> None:
    application.add_handler(CallbackQueryHandler(admin_ban_callback, pattern=rf"^{views.CALLBACK_PREFIX}:"))
