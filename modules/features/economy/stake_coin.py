import asyncio
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from core import mysql_connection, process_user
from core.command_cooldown import cooldown

# 全局锁，确保同一时间只有一个质押操作执行
lock = asyncio.Lock()


async def get_total_coins():
    row = await mysql_connection.fetch_one("SELECT SUM(coins) FROM user")
    return row[0] if row and row[0] else 0


async def get_total_staked():
    row = await mysql_connection.fetch_one("SELECT SUM(stake_amount) FROM user_stakes")
    return row[0] if row and row[0] else 0


async def calculate_reward_rate():
    total_coins = await get_total_coins()
    total_staked = await get_total_staked()

    if total_staked == 0 or total_coins == 0:
        return 3.0

    stake_ratio = min(1.0, float(total_staked) / (float(total_coins) + float(total_staked)))
    max_rate = 3.0
    min_rate = 0.5
    reward_rate = max_rate - stake_ratio * (max_rate - min_rate)

    return reward_rate


async def get_user_stake(user_id, *, connection=None):
    row = await mysql_connection.fetch_one(
        "SELECT stake_amount, stake_time, last_reward_time FROM user_stakes WHERE user_id = %s",
        (user_id,),
        connection=connection,
    )
    if not row:
        return None
    return {
        "stake_amount": row[0],
        "stake_time": row[1],
        "last_reward_time": row[2],
    }


async def calculate_available_reward(user_id):
    user_stake = await get_user_stake(user_id)
    if not user_stake or user_stake["stake_amount"] <= 0:
        return 0

    reward_rate = await calculate_reward_rate()

    last_reward_time = user_stake["last_reward_time"] or user_stake["stake_time"]
    now = datetime.now()
    hours_passed = (now - last_reward_time).total_seconds() / 3600
    days_passed = int(hours_passed / 24)

    daily_reward = int(float(user_stake["stake_amount"]) * (reward_rate / 100))
    return daily_reward * days_passed


@cooldown
async def stake_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not await process_user.async_user_exists(user_id):
        await update.message.reply_text(
            "请先使用 /me 命令注册您的账户。\n"
            "Please register first using the /me command."
        )
        return

    if not context.args:
        await show_stake_status(update, context)
        return

    try:
        amount = int(context.args[0])
        if amount <= 0:
            raise ValueError("质押金额必须为正整数")

        await stake_coins(update, context, amount)
    except ValueError:
        await update.message.reply_text(
            "请输入有效的质押金额。格式: /stake <数量>\n"
            "Please enter a valid stake amount. Format: /stake <amount>"
        )


async def show_stake_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_stake = await get_user_stake(user_id)
    reward_rate = await calculate_reward_rate()

    status_message = f"当前质押回报率: {reward_rate:.2f}%/天\n"

    if user_stake:
        available_reward = await calculate_available_reward(user_id)
        stake_time_str = user_stake["stake_time"].strftime("%Y-%m-%d %H:%M:%S")

        status_message += (
            f"您当前已质押: {user_stake['stake_amount']} 金币\n"
            f"质押时间: {stake_time_str}\n"
            f"可领取回报: {available_reward} 金币"
        )

        keyboard = [
            [InlineKeyboardButton("领取回报", callback_data=f"stake_collect_{user_id}")],
            [InlineKeyboardButton("取出本金", callback_data=f"stake_withdraw_{user_id}")],
        ]
    else:
        status_message += (
            "您当前没有质押任何金币。\n"
            "使用 /stake <数量> 命令来质押金币。"
        )
        keyboard = []

    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None

    await update.message.reply_text(status_message, reply_markup=reply_markup)


async def stake_coins(update: Update, context: ContextTypes.DEFAULT_TYPE, amount: int):
    user_id = update.effective_user.id

    async with lock:
        user_coins = await process_user.async_get_user_coins(user_id)

        if user_coins < amount:
            await update.message.reply_text(
                f"您没有足够的金币。当前余额: {user_coins} 金币。\n"
                f"You don't have enough coins. Current balance: {user_coins} coins."
            )
            return

        try:
            async with mysql_connection.transaction() as connection:
                existing_stake = await get_user_stake(user_id, connection=connection)

                if existing_stake:
                    await update.message.reply_text(
                        "您已经有质押的金币。如果要增加质押金额，请先取出当前质押。\n"
                        "You already have staked coins. If you want to increase your stake, please withdraw your current stake first."
                    )
                    return

                await connection.exec_driver_sql(
                    "UPDATE user SET coins = coins - %s WHERE id = %s",
                    (amount, user_id),
                )

                now = datetime.now()
                await connection.exec_driver_sql(
                    "INSERT INTO user_stakes (user_id, stake_amount, stake_time) VALUES (%s, %s, %s)",
                    (user_id, amount, now),
                )

            reward_rate = await calculate_reward_rate()
            await update.message.reply_text(
                f"成功质押 {amount} 金币！当前回报率为 {reward_rate:.2f}%/天。\n"
                f"每24小时可领取一次回报。\n"
                f"Successfully staked {amount} coins! Current reward rate is {reward_rate:.2f}% everyday.\n"
                f"You can collect rewards once every 24 hours."
            )
        except Exception as e:
            await update.message.reply_text(
                f"质押过程中发生错误: {str(e)}\n"
                f"Error occurred during staking: {str(e)}"
            )


async def stake_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data.split("_")
    action = data[1]
    target_user_id = int(data[2])
    user_id = update.effective_user.id

    if user_id != target_user_id:
        await query.answer("这不是你的质押，你不能操作。", show_alert=True)
        return

    if action == "collect":
        await collect_reward(query, user_id)
    elif action == "withdraw":
        await withdraw_stake(query, user_id)


async def collect_reward(query, user_id):
    async with lock:
        try:
            async with mysql_connection.transaction() as connection:
                user_stake = await get_user_stake(user_id, connection=connection)
                if not user_stake:
                    await query.answer("您没有质押任何金币。", show_alert=True)
                    return

                reward = await calculate_available_reward(user_id)

                if reward <= 0:
                    await query.answer("没有可领取的回报。需要等待至少24小时。", show_alert=True)
                    return

                await connection.exec_driver_sql(
                    "UPDATE user SET coins = coins + %s WHERE id = %s",
                    (reward, user_id),
                )

                await connection.exec_driver_sql(
                    "UPDATE user_stakes SET last_reward_time = %s WHERE user_id = %s",
                    (datetime.now(), user_id),
                )

            reward_rate = await calculate_reward_rate()
            await query.edit_message_text(
                f"您已成功领取 {reward} 金币的回报！\n"
                f"当前质押金额: {user_stake['stake_amount']} 金币\n"
                f"当前回报率: {reward_rate:.2f}%/天",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("领取回报", callback_data=f"stake_collect_{user_id}")],
                    [InlineKeyboardButton("取出本金", callback_data=f"stake_withdraw_{user_id}")],
                ]),
            )

            await query.answer(f"成功领取 {reward} 金币回报！", show_alert=True)
        except Exception as e:
            await query.answer(f"领取回报时发生错误: {str(e)}", show_alert=True)


async def withdraw_stake(query, user_id):
    async with lock:
        try:
            async with mysql_connection.transaction() as connection:
                user_stake = await get_user_stake(user_id, connection=connection)
                if not user_stake:
                    await query.answer("您没有质押任何金币。", show_alert=True)
                    return

                stake_amount = user_stake["stake_amount"]
                reward = await calculate_available_reward(user_id)

                await connection.exec_driver_sql(
                    "UPDATE user SET coins = coins + %s WHERE id = %s",
                    (stake_amount, user_id),
                )

                if reward > 0:
                    await connection.exec_driver_sql(
                        "UPDATE user SET coins = coins + %s WHERE id = %s",
                        (reward, user_id),
                    )
                    msg = f"您已取出质押本金 {stake_amount} 金币，并获得回报 {reward} 金币！"
                else:
                    msg = f"您已取出质押本金 {stake_amount} 金币。\n未满24小时，无法获得回报。"

                await connection.exec_driver_sql(
                    "DELETE FROM user_stakes WHERE user_id = %s",
                    (user_id,),
                )

            reward_rate = await calculate_reward_rate()
            await query.edit_message_text(
                f"{msg}\n\n"
                f"当前质押回报率: {reward_rate:.2f}%/天\n"
                f"您目前没有质押金币。\n"
                f"使用 /stake <数量> 命令来质押金币。"
            )

            await query.answer(msg, show_alert=True)
        except Exception as e:
            await query.answer(f"取出本金时发生错误: {str(e)}", show_alert=True)


# 创建质押相关的处理器
def setup_stake_handlers(application):
    """为质押系统设置处理器"""
    application.add_handler(CommandHandler("stake", stake_command))
    application.add_handler(CallbackQueryHandler(stake_callback, pattern=r"^stake_"))
