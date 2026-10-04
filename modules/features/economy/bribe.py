"""Implement /bribe command for increasing affection by spending coins."""

import logging
import random
from dataclasses import dataclass
from enum import StrEnum
from typing import Sequence

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from core import balance, process_user
from core.command_cooldown import cooldown

BRIBE_COMMAND_ENABLED = False  # 暂时禁用 /bribe 命令


class BribeStatus(StrEnum):
    PAID = "paid"
    REPLAYED = "replayed"
    INSUFFICIENT = "insufficient"
    NOT_REGISTERED = "not_registered"


@dataclass(frozen=True)
class BribeOutcome:
    status: BribeStatus
    balance_total: int = 0
    total_gain: int = 0
    affection_after: int = 0


def bribe_op_key(chat_id: int, message_id: int) -> str:
    """贿赂扣款：以命令消息为身份，同一条 /bribe 被重复投递不会再扣一次。"""
    return balance.make_op_key("bribe", chat_id, message_id)


async def _pay_bribe(
    user_id: int,
    coins_to_spend: int,
    affection_before: int,
    op_key: str,
) -> BribeOutcome:
    """扣款与好感度变化在同一个事务里：好感度写入失败时金币一并回滚。"""

    async def work(connection) -> BribeOutcome:
        try:
            balances = await balance.lock_user(connection, user_id)
        except balance.UserNotFound:
            return BribeOutcome(BribeStatus.NOT_REGISTERED)
        if await balance.get_operation(op_key, connection=connection) is not None:
            return BribeOutcome(BribeStatus.REPLAYED)
        if balances.total < coins_to_spend:
            return BribeOutcome(BribeStatus.INSUFFICIENT, balance_total=balances.total)
        try:
            await balance.debit(
                connection, user_id, coins_to_spend, op_key=op_key, reason="bribe"
            )
        except balance.InsufficientBalance as exc:
            return BribeOutcome(BribeStatus.INSUFFICIENT, balance_total=exc.balance_total)

        current_affection = affection_before
        total_gain = 0
        for _ in range(coins_to_spend // 100):
            delta = random.randint(1, 10)
            new_affection = await process_user.update_user_affection(
                user_id, delta, connection=connection
            )
            total_gain += max(0, new_affection - current_affection)
            current_affection = new_affection
            if current_affection >= 100:
                break
        return BribeOutcome(
            BribeStatus.PAID, total_gain=total_gain, affection_after=current_affection
        )

    return await balance.run_in_transaction(work)


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
        op_key = bribe_op_key(chat_id, message_id)

    try:
        outcome = await _pay_bribe(user_id, coins_to_spend, affection_before, op_key)
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
