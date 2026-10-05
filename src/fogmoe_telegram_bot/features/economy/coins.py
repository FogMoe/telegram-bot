"""/lottery、/rich、/give 的 Telegram 适配层：输入映射与回复。

抽奖、赠送与富豪榜的规则和事务在 `operations/lottery.py` 与 `operations/coins.py`。
"""

import logging
import time
from datetime import datetime

from telegram import Update
from telegram.ext import ContextTypes

from fogmoe_telegram_bot.core.command_cooldown import cooldown
from fogmoe_telegram_bot.core.redaction import report_error

from .operations import coins as coin_operations
from .operations import lottery as lottery_operations
from .operations.coins import GIVE_DAILY_LIMIT, GiveStatus
from .operations.lottery import LotteryOutcome, LotteryStatus

logger = logging.getLogger(__name__)

last_rich_query_time = 0


def lottery_message(outcome: LotteryOutcome) -> str:
    """抽奖结果给用户看的文案。"""
    if outcome.status is LotteryStatus.WON:
        return (
            f"恭喜！您赢得了 {outcome.coins} 枚硬币喵。\n"
            f"Congratulations! You have won {outcome.coins} coins. Meow!"
        )
    if outcome.status is LotteryStatus.NOT_REGISTERED:
        return (
            "请先使用 /me 命令获取个人信息。\n"
            "Please register first using the /me command."
        )
    if outcome.status is LotteryStatus.COOLING_DOWN:
        return (
            "每24小时您只能参加一次抽奖喵。下次再来吧！\n"
            "You can only participate in the lottery once every 24 hours. Meow! Come back later!"
        )
    return (
        "抽奖操作过于频繁，请等待上一次操作完成。\n"
        "You're drawing too fast, please wait for the previous lottery to complete."
    )


@cooldown
async def lottery_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    outcome = await lottery_operations.async_lottery(user_id)
    await context.bot.send_message(
        chat_id=update.effective_chat.id, text=lottery_message(outcome)
    )


@cooldown
async def rich_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_rich_query_time
    current_time = time.time()
    if current_time - last_rich_query_time < 60:
        await update.message.reply_text("查询过于频繁，每60秒只能查询一次，请稍后再试。")
        return
    last_rich_query_time = current_time
    try:
        results = await coin_operations.richest_users(5)
    except Exception as e:
        notice = report_error(logger, "查询富豪榜时出错", e)
        await update.message.reply_text(f"查询富豪榜时出错，请稍后再试。\n{notice}")
        return

    if not results:
        await update.message.reply_text("暂无数据")
        return

    rich_list = " 富豪榜 Top 5 \n\n"
    for idx, entry in enumerate(results, start=1):
        rich_list += f"{idx}. {entry.name} - {entry.coins_total} 枚硬币\n"
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
        fee = coin_operations.calculate_give_fee(amount)
        total_cost = amount + fee
        chat_id = update.effective_chat.id
        message_id = update.message.message_id
        outcome = await coin_operations.transfer_coins(
            sender_id,
            await coin_operations.find_recipient_id(target_name),
            amount,
            sender_op_key=coin_operations.give_op_key(chat_id, message_id),
            recipient_op_key=coin_operations.give_recipient_op_key(chat_id, message_id),
            today=datetime.now().date(),
        )

        if outcome.status is GiveStatus.NOT_REGISTERED:
            await update.message.reply_text("请先使用 /me 命令注册个人信息。")
        elif outcome.status is GiveStatus.INSUFFICIENT:
            await update.message.reply_text(
                f"您的硬币不足，当前硬币：{outcome.balance_total}，需要：{total_cost}"
            )
        elif outcome.status is GiveStatus.DAILY_LIMIT:
            await update.message.reply_text(
                f"您今天的赠送次数已达上限（{GIVE_DAILY_LIMIT}次），请明天再试。"
            )
        elif outcome.status is GiveStatus.RECIPIENT_NOT_FOUND:
            await update.message.reply_text(
                f"未找到用户名为 '{target_name}' 的用户。"
            )
        elif outcome.status is GiveStatus.SELF:
            await update.message.reply_text("不能给自己赠送硬币哦~")
        elif outcome.status is GiveStatus.CONFLICT:
            await update.message.reply_text("这条赠送命令已经处理过另一笔转账，本次没有执行，请重新发送 /give。")
        elif fee > 0:
            await update.message.reply_text(
                f"成功赠送 {amount} 枚硬币给用户 {target_name}，手续费 {fee} 枚硬币。"
            )
        else:
            await update.message.reply_text(f"成功赠送 {amount} 枚硬币给用户 {target_name}。")
    except Exception:
        logger.exception("赠送硬币失败: sender_id=%s target=%s", sender_id, target_name)
        await update.message.reply_text("转账过程中出现错误，请稍后再试。")
