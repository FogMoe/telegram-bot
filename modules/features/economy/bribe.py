"""Implement /bribe command for increasing affection by spending coins."""

import logging
import random
from typing import Sequence

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from core import mysql_connection, process_user
from core.command_cooldown import cooldown


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

    affection_before = process_user.get_user_affection(user_id)
    if affection_before >= 100:
        await _reply(update, "雾萌娘已经对你满怀好感啦，再多金币也没有上限可涨了！")
        return

    connection = mysql_connection.create_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT coins FROM user WHERE id = %s", (user_id,))
        row = cursor.fetchone()
        if not row:
            await _reply(update, "请先使用 /me 命令注册个人信息。")
            return

        current_coins = row[0]
        if current_coins < coins_to_spend:
            await _reply(update, f"您的金币不足，当前拥有 {current_coins} 枚，无法支付 {coins_to_spend} 枚。")
            return

        cursor.execute(
            "UPDATE user SET coins = coins - %s WHERE id = %s",
            (coins_to_spend, user_id),
        )
        connection.commit()
    except Exception as exc:
        connection.rollback()
        logging.error("/bribe 扣除金币失败: %s", exc)
        await _reply(update, "贿赂过程中出现问题，请稍后再试。")
        return
    finally:
        cursor.close()
        connection.close()

    batches = coins_to_spend // 100
    total_gain = 0
    current_affection = affection_before

    for _ in range(batches):
        delta = random.randint(1, 10)
        new_affection = process_user.update_user_affection(user_id, delta)
        gained = max(0, new_affection - current_affection)
        total_gain += gained
        current_affection = new_affection
        if current_affection >= 100:
            break

    affection_after = current_affection

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
    application.add_handler(CommandHandler("bribe", bribe_command))
