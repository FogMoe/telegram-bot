"""Implement /bribe command for increasing affection by spending coins."""

import logging
from typing import Sequence

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from core import balance, process_user
from core.command_cooldown import cooldown

from .operations import bribe as bribe_operations
from .operations.bribe import BribeStatus

BRIBE_COMMAND_ENABLED = False  # 暂时禁用 /bribe 命令


async def _reply(update: Update, text: str) -> None:
    if update.message:
        await update.message.reply_text(text)


@cooldown
async def bribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle `/bribe <coins>` command."""
    user_id = update.effective_user.id

    args: Sequence[str] = context.args or []
    if not args:
        await _reply(update, "用法：/bribe <金币数量> （每100金币随机提升1-10好感度）")
        return

    try:
        coins_to_spend = int(args[0])
    except ValueError:
        await _reply(update, "请输入有效的金币数量，例如 /bribe 300")
        return

    if coins_to_spend <= 0:
        await _reply(update, "金币数量必须为正整数哦。")
        return

    if coins_to_spend < 100:
        await _reply(update, "至少需要 100 枚金币才能打动雾萌娘喵！")
        return

    if coins_to_spend % 100 != 0:
        await _reply(update, "为了公平起见，金币数量必须是 100 的整数倍。")
        return

    affection_before = await process_user.async_get_user_affection(user_id)
    if affection_before >= 100:
        await _reply(update, "雾萌娘已经对你满怀好感啦，再多金币也没有上限可涨了！")
        return

    chat_id = getattr(update.effective_chat, "id", None)
    message_id = getattr(update.message, "message_id", None)
    if chat_id is None or message_id is None:
        op_key = balance.new_op_key("bribe:adhoc")
    else:
        op_key = bribe_operations.bribe_op_key(chat_id, message_id)

    try:
        outcome = await bribe_operations.pay_bribe(
            user_id, coins_to_spend, affection_before, op_key
        )
    except Exception as exc:
        logging.error("/bribe 扣除金币失败: %s", exc)
        await _reply(update, "贿赂过程中出现问题，请稍后再试。")
        return

    if outcome.status is BribeStatus.NOT_REGISTERED:
        await _reply(update, "请先使用 /me 命令注册个人信息。")
        return
    if outcome.status is BribeStatus.INSUFFICIENT:
        await _reply(
            update,
            f"您的金币不足，当前拥有 {outcome.balance_total} 枚，无法支付 {coins_to_spend} 枚。",
        )
        return
    if outcome.status is BribeStatus.REPLAYED:
        # 同一条命令被重复投递：上一次已经扣款并写入好感度，不再重复处理。
        return

    total_gain = outcome.total_gain
    affection_after = outcome.affection_after

    if total_gain <= 0:
        await _reply(update, "雾萌娘的好感度已经拉满啦，再多金币也收不下了！")
        return

    await _reply(
        update,
        (
            f"雾萌娘收下了 {coins_to_spend} 枚金币，心情改善了 {total_gain} 点！\n"
            f"当前好感度：{affection_before} → {affection_after}"
        ),
    )


def setup_bribe_command(application: Application) -> None:
    if not BRIBE_COMMAND_ENABLED:
        return
    application.add_handler(CommandHandler("bribe", bribe_command))
