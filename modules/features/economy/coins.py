import logging
import time
from datetime import datetime

from telegram import Update
from telegram.ext import ContextTypes

from core import mysql_connection, process_user
from core.command_cooldown import cooldown

logger = logging.getLogger(__name__)

last_rich_query_time = 0
GIVE_DAILY_LIMIT = 5


def _calculate_give_fee(amount: int) -> int:
    if amount <= 1:
        return 0
    fee = amount // 5
    return fee if fee >= 1 else 1


@cooldown
async def lottery_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    result = await process_user.async_lottery(user_id)
    await context.bot.send_message(chat_id=update.effective_chat.id, text=result)


@cooldown
async def rich_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_rich_query_time
    current_time = time.time()
    if current_time - last_rich_query_time < 60:
        await update.message.reply_text("查询过于频繁，每60秒只能查询一次，请稍后再试。")
        return
    last_rich_query_time = current_time
    try:
        query = "SELECT name, (coins + coins_paid) AS coins_total FROM user ORDER BY coins_total DESC LIMIT 5"
        results = await mysql_connection.fetch_all(query)
    except Exception as e:
        await update.message.reply_text(f"查询富豪榜时出错：{str(e)}")
        return

    if not results:
        await update.message.reply_text("暂无数据")
        return

    rich_list = " 富豪榜 Top 5 \n\n"
    for idx, (name, coins) in enumerate(results, start=1):
        rich_list += f"{idx}. {name} - {coins} 枚硬币\n"
    await update.message.reply_text(rich_list)


@cooldown
async def give_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /give <name> <num>
    赠送硬币：
    - name 为数据库表 user 中的 name 字段（目标用户）的值
    - num 为赠送的硬币数
    """
    if len(context.args) != 2:
        await update.message.reply_text("用法：/give <用户名> <数量>\n严禁恶意刷硬币、出售，违规者将被封禁！")
        return

    target_name = context.args[0]
    try:
        amount = int(context.args[1])
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("赠送数量必须为正整数！")
        return

    sender_id = update.effective_user.id

    try:
        fee = _calculate_give_fee(amount)
        total_cost = amount + fee
        async with mysql_connection.transaction() as connection:
            sender_row = await mysql_connection.fetch_one(
                "SELECT coins, coins_paid FROM user WHERE id = %s",
                (sender_id,),
                connection=connection,
            )
            if not sender_row:
                await update.message.reply_text("请先使用 /me 命令注册个人信息。")
                return
            sender_coins = (sender_row[0] or 0) + (sender_row[1] or 0)
            if sender_coins < total_cost:
                await update.message.reply_text(
                    f"您的硬币不足，当前硬币：{sender_coins}，需要：{total_cost}"
                )
                return

            today = datetime.now().date()
            give_row = await mysql_connection.fetch_one(
                "SELECT give_count FROM user_give_daily WHERE user_id = %s AND give_date = %s FOR UPDATE",
                (sender_id, today),
                connection=connection,
            )
            current_count = give_row[0] if give_row else 0
            if current_count >= GIVE_DAILY_LIMIT:
                await update.message.reply_text(
                    f"您今天的赠送次数已达上限（{GIVE_DAILY_LIMIT}次），请明天再试。"
                )
                return

            recipient_row = await mysql_connection.fetch_one(
                "SELECT id FROM user WHERE name = %s",
                (target_name,),
                connection=connection,
            )
            if not recipient_row:
                await update.message.reply_text(
                    f"未找到用户名为 '{target_name}' 的用户。"
                )
                return
            recipient_id = recipient_row[0]

            if sender_id == recipient_id:
                await update.message.reply_text("不能给自己赠送硬币哦~")
                return

            spent = await process_user.spend_user_coins(
                sender_id,
                total_cost,
                connection=connection,
            )
            if not spent:
                await update.message.reply_text(
                    f"您的硬币不足，当前硬币：{sender_coins}，需要：{total_cost}"
                )
                return
            await process_user.add_free_coins(
                recipient_id,
                amount,
                connection=connection,
            )
            if give_row:
                await connection.exec_driver_sql(
                    "UPDATE user_give_daily SET give_count = give_count + 1 WHERE user_id = %s AND give_date = %s",
                    (sender_id, today),
                )
            else:
                await connection.exec_driver_sql(
                    "INSERT INTO user_give_daily (user_id, give_date, give_count) VALUES (%s, %s, 1)",
                    (sender_id, today),
                )

        if fee > 0:
            await update.message.reply_text(
                f"成功赠送 {amount} 枚硬币给用户 {target_name}，手续费 {fee} 枚硬币。"
            )
        else:
            await update.message.reply_text(f"成功赠送 {amount} 枚硬币给用户 {target_name}。")
    except Exception:
        logger.exception("赠送硬币失败: sender_id=%s target=%s", sender_id, target_name)
        await update.message.reply_text("转账过程中出现错误，请稍后再试。")
