"""/checkin 的 Telegram 适配层：输入映射与回复。签到规则与事务在 `operations/checkin.py`。"""

import html
import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import CommandHandler, ContextTypes

from fogmoe_telegram_bot.core import user_records
from fogmoe_telegram_bot.core.command_cooldown import cooldown

from .operations import checkin as checkin_operations
from .operations.checkin import MAX_REWARD_DAYS, CheckinStatus


@cooldown
async def checkin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name

    if not update.effective_user.username:
        await update.message.reply_text(
            "您需要设置Telegram用户名才能使用签到功能。\n"
            "请在Telegram设置中设置用户名后再尝试。\n\n"
            "You need to set a Telegram username to use the check-in feature.\n"
            "Please set your username in Telegram settings and try again."
        )
        return

    escaped_username = html.escape(username)

    if not await user_records.async_check_user_exists(user_id):
        await update.message.reply_text(
            "请先使用 /me 命令注册账户。\n"
            "Please register first using the /me command."
        )
        return

    result = await checkin_operations.process_checkin(user_id)

    if result.status is CheckinStatus.CHECKED_IN:
        message = (
            f"🎉 <b>签到成功</b> 🎉\n\n"
            f"用户: @{escaped_username}\n"
            f"连续签到: <b>{result.consecutive_days}</b> 天\n"
            f"今日奖励: <b>{result.reward}</b> 金币\n\n"
        )

        days_left = max(MAX_REWARD_DAYS - result.consecutive_days, 0)
        if days_left > 0:
            message += f"距离最高奖励还有 {days_left} 天\n"
            progress = min(result.consecutive_days, MAX_REWARD_DAYS) / MAX_REWARD_DAYS
            progress_bar = "".join(["🟢" if i / 10 <= progress else "⚪" for i in range(1, 11)])
            message += f"{progress_bar} {int(progress * 100)}%\n\n"
        else:
            message += "恭喜！你已达到最高奖励等级！🏆\n\n"

        message += "每天签到可获得金币奖励，连续签到奖励更多！"
    else:
        message = (
            f"⚠️ 您今天已经签到过了！请明天再来。\n\n"
            f"当前连续签到: <b>{result.consecutive_days}</b> 天\n"
            f"请明天再来签到以继续你的连续签到记录！"
        )

    try:
        await update.message.reply_text(
            message,
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logging.error(f"签到消息HTML解析错误: {str(e)}")
        await update.message.reply_text(
            message.replace("<b>", "").replace("</b>", ""),
            parse_mode=None,
        )


def setup_checkin_handlers(application):
    """设置签到功能的处理器"""
    application.add_handler(CommandHandler("checkin", checkin_command))
    logging.info("签到系统已初始化")
