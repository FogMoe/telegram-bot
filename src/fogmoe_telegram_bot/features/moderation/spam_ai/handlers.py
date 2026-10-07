"""/spam ai：群管理员按 30 天付费开启 AI 垃圾识别；新成员的前几条消息交给 Jev 判断，判定为垃圾信息的删除并计入处罚。

适配层：子命令的回复、把一条 Telegram 消息整理成判断的输入、在后台执行检查、定时发到期提醒。
付费与检查规则在 `operations.py`，模型请求在 `judge.py`，警告与移出在 `features/moderation/spam_strikes.py`。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from telegram import Message, MessageEntity, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from fogmoe_telegram_bot.core import background
from fogmoe_telegram_bot.features.moderation import spam_strikes

from . import judge, operations
from .operations import PayResult, PayStatus
from .repositories.groups import ReminderKind, SpamAiGroup

logger = logging.getLogger(__name__)

PRICE = operations.PERIOD_PRICE
DAYS = operations.PERIOD_DAYS
CHECKED = operations.CHECKED_MESSAGES_PER_MEMBER
RENEW_HINT = f"/spam ai renew 续费 {DAYS} 天（{PRICE} 金币）"

USAGE = (
    f"/spam ai on - 开启 AI 识别（未付费或已到期时扣 {PRICE} 金币，有效 {DAYS} 天）\n"
    "/spam ai off - 暂停 AI 识别\n"
    f"/spam ai renew - 续费 {DAYS} 天（{PRICE} 金币）"
)
NOT_AVAILABLE = "AI 识别暂未开放。"


def format_time(value: datetime) -> str:
    return f"{value:%Y-%m-%d %H:%M} UTC"


def remaining(paid_until: datetime, now: datetime) -> str:
    days = (paid_until - now).days
    return f"还剩 {days} 天" if days >= 1 else "不到 1 天"


def status_text(
    group: SpamAiGroup | None,
    *,
    now: datetime,
    limit_reached: bool,
    available: bool,
    filter_enabled: bool,
) -> str:
    if group is None:
        if not available:
            return NOT_AVAILABLE
        return (
            f"本群尚未开通 AI 识别。开通后，每位成员接下来在本群发的 {CHECKED} 条消息会由 AI 检查，"
            f"识别为垃圾信息的会被自动删除。\n\n{USAGE}"
        )
    until = format_time(group.paid_until)
    if group.paid_until <= now:
        status = f"本群的 AI 识别已于 {until} 到期。{RENEW_HINT}。"
    elif not group.enabled:
        status = (
            f"AI 识别已暂停，有效期至 {until}（{remaining(group.paid_until, now)}）。"
            "到期前用 /spam ai on 恢复，不另收费。"
        )
    else:
        status = f"AI 识别已开启，有效期至 {until}（{remaining(group.paid_until, now)}）。"
        if not filter_enabled:
            status += "\n垃圾信息过滤总开关已关闭，AI 识别不会运行；使用 /spam 重新开启。"
        elif not available:
            status += "\nAI 识别服务暂时不可用，目前只使用关键词过滤。"
        elif limit_reached:
            status += "\n今天的检查次数已达上限，UTC 0 点恢复，在此之前只使用关键词过滤。"
    return f"{status}\n\n{USAGE}"


def pay_text(result: PayResult, *, renewing: bool) -> str:
    action = "续费" if renewing else "开通"
    if result.status is PayStatus.NOT_REGISTERED:
        return f"请先使用 /me 命令注册个人信息，再来{action}。"
    if result.status is PayStatus.INSUFFICIENT:
        return f"{action}需要 {PRICE} 金币，你当前只有 {result.balance_total} 枚。"
    until = format_time(result.paid_until) if result.paid_until else "未知"
    if result.status is PayStatus.ALREADY_ON:
        return f"AI 识别已经是开启状态，有效期至 {until}。"
    if result.status is PayStatus.RESUMED:
        return f"已恢复 AI 识别，有效期至 {until}，本次没有扣费。"
    if not result.extended:
        return (
            f"已开启 AI 识别，扣除 {PRICE} 金币，有效期至 {until}。\n"
            f"从现在起，每位成员接下来在本群发的 {CHECKED} 条消息会由 AI 检查。"
        )
    renewed = f"已续费 {DAYS} 天并恢复 AI 识别" if result.resumed else f"已续费 {DAYS} 天"
    return f"{renewed}，扣除 {PRICE} 金币，有效期延长至 {until}。"


async def _reply(update: Update, text: str) -> None:
    if update.message:
        await update.message.reply_text(text)


async def _bot_can_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool | None:
    """机器人能否删除消息；查不到时返回 None。"""
    try:
        member = await context.bot.get_chat_member(update.effective_chat.id, context.bot.id)
    except TelegramError as exc:
        logger.warning("/spam ai 检查机器人权限失败: %s", exc)
        return None
    return bool(getattr(member, "can_delete_messages", False))


async def _pay(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    renewing: bool,
    filter_enabled: bool,
) -> None:
    if not judge.is_configured():
        await _reply(update, NOT_AVAILABLE)
        return
    if not filter_enabled:
        await _reply(update, "请先开启垃圾信息过滤功能（使用 /spam 命令），才能开启 AI 识别。")
        return
    can_delete = await _bot_can_delete(update, context)
    if can_delete is None:
        await _reply(update, "检查机器人权限时出错，请稍后再试。")
        return
    if not can_delete:
        await _reply(update, "机器人需要有删除消息的权限才能使用 AI 识别。")
        return

    chat_id = update.effective_chat.id
    op_key = operations.payment_op_key(chat_id, update.message.message_id)
    action = operations.renew if renewing else operations.enable
    try:
        result = await action(chat_id, update.effective_user.id, op_key)
    except Exception:
        logger.exception("/spam ai %s 失败（chat %s）", "renew" if renewing else "on", chat_id)
        await _reply(update, f"{'续费' if renewing else '开通'}时出现问题，请稍后再试。")
        return
    await _reply(update, pay_text(result, renewing=renewing))


async def _pause(update: Update) -> None:
    group = await operations.pause(update.effective_chat.id)
    if group is None:
        await _reply(update, "本群当前没有开启 AI 识别。")
        return
    await _reply(
        update,
        f"已暂停 AI 识别。有效期仍到 {format_time(group.paid_until)}，暂停期间照常计算；"
        "到期前用 /spam ai on 恢复，不另收费。",
    )


async def spam_ai_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    args: Sequence[str],
    *,
    filter_enabled: bool,
) -> None:
    """`/spam ai ...`；调用方已经确认是群组、发命令的是管理员。"""
    if update.message is None:
        # 编辑过的命令消息不再执行一次：编辑后的 renew 会被当成一次新的付费
        return
    sub_command = args[0].lower() if args else ""
    try:
        if sub_command == "":
            chat_id = update.effective_chat.id
            group = await operations.get_status(chat_id)
            await _reply(
                update,
                status_text(
                    group,
                    now=operations.utcnow(),
                    limit_reached=operations.daily_limit_reached(chat_id),
                    available=judge.is_configured(),
                    filter_enabled=filter_enabled,
                ),
            )
        elif sub_command in ("on", "renew"):
            await _pay(update, context, renewing=sub_command == "renew", filter_enabled=filter_enabled)
        elif sub_command == "off":
            await _pause(update)
        else:
            await _reply(update, f"用法：\n/spam ai - 查看 AI 识别状态\n{USAGE}")
    except Exception:
        logger.exception("/spam ai 处理失败")
        await _reply(update, "操作失败，请稍后再试。")


# ---------------------------------------------------------------------------
# 检查群消息
# ---------------------------------------------------------------------------


def hidden_links(message: Message) -> tuple[str, ...]:
    """文字链接背后的网址（正文里看不到）。"""
    entities = message.caption_entities if message.caption else message.entities
    return tuple(
        entity.url for entity in entities or () if entity.type == MessageEntity.TEXT_LINK and entity.url
    )


def _origin_name(message: Message) -> str | None:
    origin: Any = message.forward_origin
    if origin is None:
        return None
    for chat in (getattr(origin, "chat", None), getattr(origin, "sender_chat", None)):
        if chat is not None and chat.title:
            return str(chat.title)
    user = getattr(origin, "sender_user", None)
    if user is not None:
        return str(user.full_name)
    hidden_name = getattr(origin, "sender_user_name", None)
    return str(hidden_name) if hidden_name else "未知来源"


def review_input(message: Message) -> judge.MessageForReview:
    reply = message.reply_to_message
    reply_text = (reply.text or reply.caption) if reply is not None else None
    user = message.from_user
    return judge.MessageForReview(
        text=message.text or message.caption or "",
        is_caption=message.text is None,
        sender_name=user.full_name if user else "",
        sender_username=user.username if user else None,
        group_title=message.chat.title,
        forwarded_from=_origin_name(message),
        hidden_links=hidden_links(message),
        reply_to_text=reply_text,
    )


def should_review(message: Message) -> bool:
    """只检查真人以个人身份发的消息：频道自动转发、以频道或群身份发言、机器人的消息都不检查。"""
    if message.is_automatic_forward or message.sender_chat is not None:
        return False
    user = message.from_user
    return user is not None and not user.is_bot


async def maybe_review(message: Message, bot: Any) -> None:
    """关键词没有命中的消息：本群开着 AI 识别、发送者还没满额时，在后台交给 AI 检查。"""
    if not judge.is_configured() or not should_review(message):
        return
    chat_id = message.chat_id
    user_id = message.from_user.id
    if operations.is_exempt(chat_id, user_id):
        return
    if not await operations.is_checking(chat_id):
        return
    background.spawn(review_message(message, bot), name=f"spam-ai-{chat_id}-{message.message_id}")


async def review_message(message: Message, bot: Any) -> None:
    chat_id = message.chat_id
    user_id = message.from_user.id
    if not await operations.needs_review(chat_id, user_id):
        return
    if not operations.take_daily_check(chat_id):
        logger.info("AI 垃圾识别：群 %s 今天的检查次数已达上限", chat_id)
        return
    try:
        judgment = await judge.judge(review_input(message))
    except judge.JudgeError as exc:
        logger.warning("AI 垃圾识别：群 %s 消息 %s 判断失败，放行: %s", chat_id, message.message_id, exc)
        return
    spam = operations.is_spam(judgment.spam_probability)
    logger.info(
        "AI 垃圾识别：群 %s 用户 %s 消息 %s 概率 %.3f（%s）模型 %s 输入 %s token",
        chat_id,
        user_id,
        message.message_id,
        judgment.spam_probability,
        "删除" if spam else "放行",
        judgment.model,
        judgment.input_tokens,
    )
    if not spam:
        await operations.record_clean(chat_id, user_id)
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message.message_id)
    except TelegramError as exc:
        logger.warning("AI 垃圾识别：删除群 %s 的消息 %s 失败: %s", chat_id, message.message_id, exc)
        return
    await spam_strikes.penalize(
        bot,
        message,
        reason_html="的消息被 AI 识别为垃圾信息",
        note="如果是误判，请联系管理员。",
    )


async def migrate_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """普通群升级成超级群时，旧群收到带 `migrate_to_chat_id` 的服务消息，新群收到带 `migrate_from_chat_id` 的；
    两条都可能到达，搬过一次之后另一条什么也不做。"""
    message = update.effective_message
    if message is None:
        return
    if message.migrate_to_chat_id:
        old_chat_id, new_chat_id = message.chat_id, message.migrate_to_chat_id
    elif message.migrate_from_chat_id:
        old_chat_id, new_chat_id = message.migrate_from_chat_id, message.chat_id
    else:
        return
    try:
        moved = await operations.move_group(old_chat_id, new_chat_id)
    except Exception:
        logger.exception("AI 垃圾识别：群 %s 升级为 %s 时搬迁付费状态失败", old_chat_id, new_chat_id)
        return
    if moved:
        logger.info("AI 垃圾识别：群 %s 升级为 %s，付费状态已搬迁", old_chat_id, new_chat_id)


# ---------------------------------------------------------------------------
# 到期提醒
# ---------------------------------------------------------------------------


def reminder_text(kind: ReminderKind, paid_until: datetime) -> str:
    if kind is ReminderKind.EXPIRING:
        return f"本群的 AI 识别将于 {format_time(paid_until)} 到期。管理员可以用 {RENEW_HINT}。"
    return f"本群的 AI 识别已到期，现在只使用关键词过滤。管理员可以用 {RENEW_HINT}。"


async def remind_expiry_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    for kind in (ReminderKind.EXPIRING, ReminderKind.EXPIRED):
        try:
            due = await operations.due_reminders(kind)
        except Exception:
            logger.exception("AI 垃圾识别：读取到期提醒失败")
            continue
        for reminder in due:
            try:
                if not await operations.claim_reminder(kind, reminder):
                    continue
                await context.bot.send_message(reminder.chat_id, reminder_text(kind, reminder.paid_until))
            except TelegramError as exc:
                logger.info("AI 垃圾识别：群 %s 的到期提醒发送失败: %s", reminder.chat_id, exc)
            except Exception:
                logger.exception("AI 垃圾识别：群 %s 的到期提醒失败", reminder.chat_id)
