"""/stake 的 Telegram 适配层：输入映射与回复。质押规则与事务在 `operations/stake.py`。"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from fogmoe_telegram_bot.core import process_user
from fogmoe_telegram_bot.core.command_cooldown import cooldown
from fogmoe_telegram_bot.core.redaction import report_error

from .operations import stake as stake_operations
from .operations.stake import (
    REWARD_INTERVAL_DAYS,
    WITHDRAW_FEE_RATE,
    CollectStatus,
    OpenStatus,
    WithdrawOutcome,
    WithdrawStatus,
)


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
    user_stake = await stake_operations.get_user_stake(user_id)
    reward_rate = await stake_operations.calculate_reward_rate()

    status_message = f"当前质押回报率: {reward_rate:.2f}%/天\n"
    status_message += f"回报按天累计，每{REWARD_INTERVAL_DAYS}天可领取一次。\n"
    status_message += f"取出本金将收取 {int(WITHDRAW_FEE_RATE * 100)}% 手续费。\n"

    if user_stake:
        available_reward = await stake_operations.calculate_available_reward(user_id)
        stake_time_str = user_stake.stake_time.strftime("%Y-%m-%d %H:%M:%S")

        status_message += (
            f"您当前已质押: {user_stake.stake_amount} 金币\n"
            f"质押时间: {stake_time_str}\n"
            f"可领取回报: {available_reward} 金币"
        )

        reply_markup = _stake_menu(user_id)
    else:
        status_message += (
            "您当前没有质押任何金币。\n"
            "使用 /stake <数量> 命令来质押金币。"
        )
        reply_markup = None

    await update.message.reply_text(status_message, reply_markup=reply_markup)


def withdraw_message(outcome: WithdrawOutcome) -> str:
    """赎回成功后给用户看的说明：本金、手续费，以及回报发放或未发放的原因。"""
    base = f"您已取出质押本金 {outcome.principal} 金币（手续费 {outcome.fee} 金币）"
    if outcome.reward > 0:
        return f"{base}，并获得回报 {outcome.reward} 金币！"
    if outcome.reward_due > 0 and outcome.intervals_passed > 0:
        return f"{base}。\n奖励池余额不足，本次未发放回报。"
    if outcome.intervals_passed > 0:
        return (
            f"{base}。\n"
            f"已满{REWARD_INTERVAL_DAYS}天，但累计回报不足 1 金币，无法获得回报。"
        )
    return f"{base}。\n未满{REWARD_INTERVAL_DAYS}天，无法获得回报。"


def _stake_menu(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("领取回报", callback_data=f"stake_collect_{user_id}")],
        [InlineKeyboardButton("取出本金", callback_data=f"stake_withdraw_{user_id}")],
    ])


async def stake_coins(update: Update, context: ContextTypes.DEFAULT_TYPE, amount: int):
    user_id = update.effective_user.id
    message = update.message

    try:
        outcome = await stake_operations.open_stake(
            user_id,
            amount,
            op_key=stake_operations.stake_open_op_key(update.effective_chat.id, message.message_id),
        )
        if outcome.status is OpenStatus.NOT_REGISTERED:
            await message.reply_text(
                "请先使用 /me 命令注册您的账户。\n"
                "Please register first using the /me command."
            )
            return
        if outcome.status is OpenStatus.INSUFFICIENT:
            await message.reply_text(
                f"您没有足够的金币。当前余额: {outcome.balance_total} 金币。\n"
                f"You don't have enough coins. Current balance: {outcome.balance_total} coins."
            )
            return
        if outcome.status is OpenStatus.ALREADY_STAKED:
            await message.reply_text(
                "您已经有质押的金币。如果要增加质押金额，请先取出当前质押。\n"
                "You already have staked coins. If you want to increase your stake, please withdraw your current stake first."
            )
            return

        reward_rate = await stake_operations.calculate_reward_rate()
        await message.reply_text(
            f"成功质押 {amount} 金币！当前回报率为 {reward_rate:.2f}%/天。\n"
            f"每{REWARD_INTERVAL_DAYS}天可领取一次回报。\n"
            f"Successfully staked {amount} coins! Current reward rate is {reward_rate:.2f}% everyday.\n"
            f"You can collect rewards once every {REWARD_INTERVAL_DAYS} days."
        )
    except Exception as e:
        notice = report_error(logging.getLogger(__name__), "质押过程中发生错误", e)
        await message.reply_text(
            f"质押过程中发生错误，请稍后再试。\n"
            f"Error occurred during staking. Please try again later.\n"
            f"{notice}"
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
    try:
        outcome = await stake_operations.collect_stake_reward(user_id)
        if outcome.status is CollectStatus.NO_STAKE:
            await query.answer("您没有质押任何金币。", show_alert=True)
            return
        if outcome.status is CollectStatus.NOT_YET:
            await query.answer(
                f"没有可领取的回报。需要等待至少{REWARD_INTERVAL_DAYS}天。",
                show_alert=True,
            )
            return
        if outcome.status is CollectStatus.TOO_SMALL:
            await query.answer(
                f"已满{REWARD_INTERVAL_DAYS}天，但累计回报不足 1 金币，继续质押会继续累计。",
                show_alert=True,
            )
            return
        if outcome.status is CollectStatus.POOL_EMPTY:
            await query.answer("奖励池余额不足，暂时无法发放回报。", show_alert=True)
            return

        reward_rate = await stake_operations.calculate_reward_rate()
        await query.edit_message_text(
            f"您已成功领取 {outcome.reward} 金币的回报！\n"
            f"当前质押金额: {outcome.stake_amount} 金币\n"
            f"当前回报率: {reward_rate:.2f}%/天",
            reply_markup=_stake_menu(user_id),
        )

        await query.answer(f"成功领取 {outcome.reward} 金币回报！", show_alert=True)
    except Exception as e:
        notice = report_error(logging.getLogger(__name__), "领取回报时发生错误", e)
        await query.answer(f"领取回报时发生错误，请稍后再试。\n{notice}", show_alert=True)


async def withdraw_stake(query, user_id):
    try:
        outcome = await stake_operations.withdraw_stake_principal(user_id)
        if outcome.status is WithdrawStatus.NO_STAKE:
            await query.answer("您没有质押任何金币。", show_alert=True)
            return

        msg = withdraw_message(outcome)
        reward_rate = await stake_operations.calculate_reward_rate()
        await query.edit_message_text(
            f"{msg}\n\n"
            f"当前质押回报率: {reward_rate:.2f}%/天\n"
            f"您目前没有质押金币。\n"
            f"使用 /stake <数量> 命令来质押金币。"
        )

        await query.answer(msg, show_alert=True)
    except Exception as e:
        notice = report_error(logging.getLogger(__name__), "取出本金时发生错误", e)
        await query.answer(f"取出本金时发生错误，请稍后再试。\n{notice}", show_alert=True)


# 创建质押相关的处理器
def setup_stake_handlers(application):
    """为质押系统设置处理器"""
    application.add_handler(CommandHandler("stake", stake_command))
    application.add_handler(CallbackQueryHandler(stake_callback, pattern=r"^stake_"))
