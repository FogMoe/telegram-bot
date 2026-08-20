import html
import logging
from datetime import datetime, timedelta

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import CommandHandler, ContextTypes

from core import mysql_connection, process_user
from core.command_cooldown import cooldown


def calculate_checkin_reward(consecutive_days):
    if consecutive_days <= 5:
        return 1
    if consecutive_days <= 10:
        return 2
    if consecutive_days <= 15:
        return 3
    if consecutive_days <= 20:
        return 4
    if consecutive_days <= 25:
        return 5
    if consecutive_days <= 30:
        return 6
    return 7


async def get_user_checkin_info(user_id):
    row = await mysql_connection.fetch_one(
        "SELECT last_checkin_date, consecutive_days FROM user_checkin WHERE user_id = %s",
        (user_id,),
    )
    return row


async def update_user_checkin(user_id, consecutive_days):
    today = datetime.now().date()
    await mysql_connection.execute(
        """
        INSERT INTO user_checkin (user_id, last_checkin_date, consecutive_days)
        VALUES (%s, %s, %s)
        ON DUPLICATE KEY UPDATE last_checkin_date = VALUES(last_checkin_date), consecutive_days = VALUES(consecutive_days)
        """,
        (user_id, today, consecutive_days),
    )


async def process_checkin(user_id):
    today = datetime.now().date()
    checkin_info = await get_user_checkin_info(user_id)

    if checkin_info and checkin_info[0] == today:
        return {
            "success": False,
            "message": "您今天已经签到过了！请明天再来。",
            "consecutive_days": checkin_info[1],
        }

    consecutive_days = 1
    if checkin_info:
        last_checkin_date = checkin_info[0]
        if last_checkin_date == today - timedelta(days=1):
            consecutive_days = checkin_info[1] + 1

    reward_coins = calculate_checkin_reward(consecutive_days)
    await update_user_checkin(user_id, consecutive_days)
    await process_user.async_update_user_coins(user_id, reward_coins)

    return {
        "success": True,
        "message": f"签到成功！\n连续签到：{consecutive_days}天\n获得奖励：{reward_coins}金币",
        "consecutive_days": consecutive_days,
        "reward": reward_coins,
    }


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

    if not await mysql_connection.async_check_user_exists(user_id):
        await update.message.reply_text(
            "请先使用 /me 命令注册账户。\n"
            "Please register first using the /me command."
        )
        return

    result = await process_checkin(user_id)

    if result["success"]:
        message = (
            f"🎉 <b>签到成功</b> 🎉\n\n"
            f"用户: @{escaped_username}\n"
            f"连续签到: <b>{result['consecutive_days']}</b> 天\n"
            f"今日奖励: <b>{result['reward']}</b> 金币\n\n"
        )

        max_reward_days = 31
        days_left = max(max_reward_days - result["consecutive_days"], 0)
        if days_left > 0:
            message += f"距离最高奖励还有 {days_left} 天\n"
            progress = min(result["consecutive_days"], max_reward_days) / max_reward_days
            progress_bar = "".join(["🟢" if i / 10 <= progress else "⚪" for i in range(1, 11)])
            message += f"{progress_bar} {int(progress * 100)}%\n\n"
        else:
            message += "恭喜！你已达到最高奖励等级！🏆\n\n"

        message += "每天签到可获得金币奖励，连续签到奖励更多！"
    else:
        message = (
            f"⚠️ {result['message']}\n\n"
            f"当前连续签到: <b>{result['consecutive_days']}</b> 天\n"
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
