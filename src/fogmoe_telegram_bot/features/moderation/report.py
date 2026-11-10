"""/report：群成员回复一条消息举报垃圾信息。凑够票数后交给 Jev 判断，判定为垃圾信息的删除消息、移出群组并封禁
24 小时；管理员在第一次举报和处理结果时各收到一条私聊，可以在私聊里一键删除并永久封禁，或者解除封禁
（按钮由 `admin_ban.py` 处理）。

适配层：校验举报、调用 Jev、删除与封禁、私聊管理员。票数与额度的规则在 `operations/report_votes.py`，
文案在 `report_views.py`，Jev 请求在 `spam_ai/judge.py`，移出在 `spam_strikes.py`。
"""

from __future__ import annotations

import html
import logging
import time
from collections.abc import Sequence
from typing import Any

from telegram import InlineKeyboardMarkup, LinkPreviewOptions, Message, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import CommandHandler, ContextTypes

from fogmoe_telegram_bot.core.command_cooldown import cooldown
from fogmoe_telegram_bot.features.moderation import admin_ban, spam_strikes
from fogmoe_telegram_bot.features.moderation import report_views as views
from fogmoe_telegram_bot.features.moderation.operations import report_votes as votes
from fogmoe_telegram_bot.features.moderation.operations.report_votes import Quota, Stage, VoteStatus
from fogmoe_telegram_bot.features.moderation.report_views import ReportedMessage
from fogmoe_telegram_bot.features.moderation.spam_ai import handlers as spam_ai_handlers
from fogmoe_telegram_bot.features.moderation.spam_ai import judge
from fogmoe_telegram_bot.features.moderation.spam_ai import operations as spam_ai_operations

logger = logging.getLogger(__name__)

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


def can_act(member: Any) -> bool:
    """机器人能删除消息并封禁成员。"""
    return member is not None and bool(getattr(member, "can_delete_messages", False)) and bool(
        getattr(member, "can_restrict_members", False)
    )


def reported_message(message: Message) -> ReportedMessage:
    chat = message.chat
    # 访客 bot 的回复算 @ 它的人（或频道）发的
    chat_sender = spam_strikes.sender_chat(message)
    if chat_sender is not None:
        sender_id, sender_html = None, html.escape(chat_sender.title or "某频道")
    else:
        sender = spam_strikes.sender_user(message)
        sender_id, sender_html = sender.id, sender.mention_html()
    return ReportedMessage(
        chat_id=chat.id,
        chat_title=chat.title or "",
        chat_username=chat.username,
        message_id=message.message_id,
        sender_id=sender_id,
        sender_html=sender_html,
        text=message.text or message.caption or "",
    )


async def _notify(bot: Any, admin_ids: Sequence[int], text: str, keyboard: InlineKeyboardMarkup | None) -> int:
    """私聊每位管理员，返回发送成功的人数。没私聊过机器人的管理员收不到。"""
    sent = 0
    for admin_id in admin_ids:
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                link_preview_options=NO_PREVIEW,
            )
            sent += 1
        except TelegramError as exc:
            logger.info("举报私聊管理员 %s 失败: %s", admin_id, exc)
    return sent


async def _member_count(bot: Any, chat_id: int) -> int | None:
    try:
        return await bot.get_chat_member_count(chat_id)
    except TelegramError as exc:
        logger.warning("举报：读取群 %s 的成员数失败: %s", chat_id, exc)
        return None


async def _paid(chat_id: int) -> bool:
    try:
        return await spam_ai_operations.is_checking(chat_id)
    except Exception:
        logger.exception("举报：读取群 %s 的 AI 识别状态失败，按未开通处理", chat_id)
        return False


def _ban_until() -> int:
    """刚封禁的人到期的 Unix 时间，和 `spam_strikes.kick` 的封禁时长一致。"""
    return int(time.time()) + spam_strikes.BAN_SECONDS


@cooldown
async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理 /report 命令：回复一条消息来举报它。"""
    message = update.message
    if message is None:
        return
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await message.reply_text(views.NOT_GROUP)
        return
    target = message.reply_to_message
    if target is None:
        await message.reply_text(views.NO_REPLY)
        return
    if message.sender_chat is not None:
        # 匿名管理员和以频道身份发言的人共用 Telegram 的同一个账号，没法按人计票和算额度
        await message.reply_text(views.AS_CHAT)
        return

    reporter = message.from_user
    # 访客 bot 的回复算 @ 它的人（或频道）发的，举报的是那个人
    chat_sender = spam_strikes.sender_chat(target)
    sender = spam_strikes.sender_user(target)
    refusal = None
    if chat_sender is not None and chat_sender.id == chat.id:
        refusal = views.ADMIN  # 匿名管理员以群的身份发言
    elif chat_sender is None and (sender is None or sender.is_bot):
        refusal = views.BOT
    elif chat_sender is None and sender.id == reporter.id:
        refusal = views.SELF
    elif votes.too_old(target.date):
        refusal = views.TOO_OLD
    if refusal:
        await message.reply_text(refusal)
        return

    try:
        admins = await context.bot.get_chat_administrators(chat.id)
    except TelegramError as exc:
        logger.error("举报：获取群 %s 的管理员失败: %s", chat.id, exc)
        await message.reply_text(views.LOOKUP_FAILED)
        return
    if chat_sender is None and any(admin.user.id == sender.id for admin in admins):
        await message.reply_text(views.ADMIN)
        return
    admin_ids = [admin.user.id for admin in admins if not admin.user.is_bot]
    bot_member = next((admin for admin in admins if admin.user.id == context.bot.id), None)
    reported = reported_message(target)
    reporter_html = reporter.mention_html()

    if reported.sender_id is None:
        # 以频道身份发的消息：没有可以处罚的个人，只通知管理员
        vote = votes.BOOK.add(chat.id, target.message_id, reporter.id)
        if vote.status is VoteStatus.DUPLICATE:
            await message.reply_text(views.DUPLICATE)
            return
        votes.BOOK.finish(chat.id, target.message_id)
        notified = None
        if vote.first:
            notified = await _notify(
                context.bot,
                admin_ids,
                views.first_report_dm(reported, reporter_html, None),
                views.pending_keyboard(reported),
            )
        await message.reply_text(views.channel_reply(vote.first, notified))
        return

    required = votes.required_votes(await _member_count(context.bot, chat.id))
    vote = votes.BOOK.add(chat.id, target.message_id, reporter.id)
    if vote.status is VoteStatus.DUPLICATE:
        await message.reply_text(views.DUPLICATE)
        return

    if vote.stage is Stage.COLLECTING and vote.count >= required and votes.BOOK.claim(chat.id, target.message_id):
        try:
            reply = await _judge_and_act(
                context.bot,
                target,
                reported,
                reporter_id=reporter.id,
                reporter_html=reporter_html,
                first=vote.first,
                count=vote.count,
                required=required,
                admin_ids=admin_ids,
                bot_member=bot_member,
            )
        except Exception:
            logger.exception("举报：处理群 %s 的消息 %s 失败", chat.id, target.message_id)
            votes.BOOK.finish(chat.id, target.message_id)
            reply = views.LOOKUP_FAILED
        if reply:
            await message.reply_text(reply)
        return

    notified = None
    if vote.first:
        notified = await _notify(
            context.bot,
            admin_ids,
            views.first_report_dm(reported, reporter_html, required),
            views.pending_keyboard(reported),
        )
    await message.reply_text(views.vote_reply(vote, required, notified))


async def _judge_and_act(
    bot: Any,
    target: Message,
    reported: ReportedMessage,
    *,
    reporter_id: int,
    reporter_html: str,
    first: bool,
    count: int,
    required: int,
    admin_ids: Sequence[int],
    bot_member: Any,
) -> str | None:
    """这次举报凑够了票数并接手了判断。返回给举报人的回复；移出成功时群里已有公告，返回 None。"""
    chat_id, message_id = reported.chat_id, reported.message_id
    reason = None
    if not judge.is_configured():
        reason = views.REASON_AI_OFF
    elif not can_act(bot_member):
        reason = views.REASON_NO_RIGHTS
    else:
        quota = votes.QUOTA.take(chat_id, reporter_id, paid=await _paid(chat_id))
        if quota is Quota.REPORTER_LIMIT:
            votes.BOOK.release(chat_id, message_id)
            notified = None
            if first:
                notified = await _notify(
                    bot,
                    admin_ids,
                    views.first_report_dm(reported, reporter_html, required),
                    views.pending_keyboard(reported),
                )
            return views.reporter_limit_reply(count, required, notified)
        if quota is Quota.GROUP_LIMIT:
            reason = views.REASON_GROUP_LIMIT
        elif quota is Quota.TOTAL_LIMIT:
            reason = views.REASON_TOTAL_LIMIT

    judgment = None
    if reason is None:
        try:
            bio = await spam_ai_handlers.sender_bio(bot, target)
            judgment = await judge.judge(spam_ai_handlers.review_input(target, bio))
        except judge.JudgeError as exc:
            logger.warning("举报：群 %s 消息 %s 的 AI 判断失败: %s", chat_id, message_id, exc)
            reason = views.REASON_JUDGE_FAILED
    votes.BOOK.finish(chat_id, message_id)

    if judgment is None:
        notified = await _notify(
            bot, admin_ids, views.escalated_dm(reported, count, reason), views.pending_keyboard(reported)
        )
        return views.escalated_reply(count, required, reason, notified)

    probability = judgment.spam_probability
    spam = spam_ai_operations.is_spam(probability)
    logger.info(
        "举报：群 %s 用户 %s 消息 %s 经 %s 人举报，AI 概率 %.3f（%s）模型 %s 输入 %s token",
        chat_id,
        reported.sender_id,
        message_id,
        count,
        probability,
        "封禁" if spam else "转交管理员",
        judgment.model,
        judgment.input_tokens,
    )
    if not spam:
        notified = await _notify(
            bot, admin_ids, views.not_spam_dm(reported, count, probability), views.pending_keyboard(reported)
        )
        return views.not_spam_reply(count, required, notified)

    deleted = await admin_ban.delete_message(bot, chat_id, message_id)
    sender = spam_strikes.sender_html(target)  # 群里的公告不显示名字
    banned = await spam_strikes.kick(
        bot,
        chat_id,
        reported.sender_id,
        sender,
        basic_group=admin_ban.is_basic_group(chat_id),
        offense=views.vote_offense(sender, count),
    )
    if banned:
        keyboard = views.banned_keyboard(
            chat_id, reported.sender_id, _ban_until(), None if deleted else reported.link
        )
    else:
        keyboard = views.pending_keyboard(reported)  # 可以在私聊里再试一次
    await _notify(
        bot,
        admin_ids,
        views.auto_banned_dm(reported, count, probability, banned=banned, deleted=deleted),
        keyboard,
    )
    return None


def setup_report_handlers(application):
    """注册处理函数"""
    application.add_handler(CommandHandler("report", report_command))
    logging.info("已加载举报功能处理器")
