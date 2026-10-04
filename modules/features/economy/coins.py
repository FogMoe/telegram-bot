import logging
import time
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from telegram import Update
from telegram.ext import ContextTypes

from core import balance, mysql_connection, process_user
from core.command_cooldown import cooldown
from core.redaction import report_error

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
        notice = report_error(logger, "查询富豪榜时出错", e)
        await update.message.reply_text(f"查询富豪榜时出错，请稍后再试。\n{notice}")
        return

    if not results:
        await update.message.reply_text("暂无数据")
        return

    rich_list = " 富豪榜 Top 5 \n\n"
    for idx, (name, coins) in enumerate(results, start=1):
        rich_list += f"{idx}. {name} - {coins} 枚硬币\n"
    await update.message.reply_text(rich_list)


class GiveStatus(StrEnum):
    GIVEN = "given"
    REPLAYED = "replayed"  # 同一条命令被重复投递，上一次已经完整执行
    NOT_REGISTERED = "not_registered"
    INSUFFICIENT = "insufficient"
    DAILY_LIMIT = "daily_limit"
    RECIPIENT_NOT_FOUND = "recipient_not_found"
    SELF = "self"


@dataclass(frozen=True)
class GiveOutcome:
    status: GiveStatus
    balance_total: int = 0  # 余额不足时发送者当前的余额


def give_op_key(chat_id: int, message_id: int) -> str:
    """赠送的身份：命令消息。发送者的扣款（含手续费）用它，收款人的入账再加 `:recv`。"""
    return balance.make_op_key("give", chat_id, message_id)


def give_recipient_op_key(chat_id: int, message_id: int) -> str:
    return balance.make_op_key("give", chat_id, message_id, "recv")


async def find_recipient_id(target_name: str) -> int | None:
    row = await mysql_connection.fetch_one(
        "SELECT id FROM user WHERE name = %s",
        (target_name,),
    )
    return int(row[0]) if row else None


async def transfer_coins(
    sender_id: int,
    recipient_id: int | None,
    amount: int,
    *,
    sender_op_key: str,
    recipient_op_key: str,
    today: date,
) -> GiveOutcome:
    """赠送：发送者扣款（本金 + 手续费）、收款人入账、每日次数在同一个事务里。

    收款人 id 在事务之前按名字解析出来，这样才能在事务开头按 user id 升序同时锁住双方
    （A 赠 B 与 B 赠 A 同时发生也不会互相等待）。每日次数在锁内读取与累加，所以并发的
    多次赠送不会突破上限。检查顺序与旧实现一致：未注册、余额不足、次数上限、收款人不存在、自赠。
    """
    fee = _calculate_give_fee(amount)
    total_cost = amount + fee

    async def work(connection) -> GiveOutcome:
        to_lock = [sender_id]
        if recipient_id is not None:
            to_lock.append(recipient_id)
        try:
            locked = await balance.lock_users(connection, to_lock)
        except balance.UserNotFound as exc:
            if exc.user_id == sender_id:
                return GiveOutcome(GiveStatus.NOT_REGISTERED)
            return GiveOutcome(GiveStatus.RECIPIENT_NOT_FOUND)

        # 重放要最先判断：它已经计入了当天的次数，也已经花掉了余额。
        if await balance.get_operation(sender_op_key, connection=connection) is not None:
            return GiveOutcome(GiveStatus.REPLAYED)

        sender_total = locked[sender_id].total
        if sender_total < total_cost:
            return GiveOutcome(GiveStatus.INSUFFICIENT, sender_total)

        # 发送者的行已锁住，这一行只会被他自己的赠送事务读写；普通读即可，
        # 对可能不存在的键做 FOR UPDATE 会让不同用户的首次写入互相死锁。
        give_row = await mysql_connection.fetch_one(
            "SELECT give_count FROM user_give_daily WHERE user_id = %s AND give_date = %s",
            (sender_id, today),
            connection=connection,
        )
        if (give_row[0] if give_row else 0) >= GIVE_DAILY_LIMIT:
            return GiveOutcome(GiveStatus.DAILY_LIMIT)
        if recipient_id is None:
            return GiveOutcome(GiveStatus.RECIPIENT_NOT_FOUND)
        if recipient_id == sender_id:
            return GiveOutcome(GiveStatus.SELF)

        try:
            await balance.debit(
                connection,
                sender_id,
                total_cost,
                op_key=sender_op_key,
                reason="give",
                ref=f"to:{recipient_id}",
            )
        except balance.InsufficientBalance as exc:
            return GiveOutcome(GiveStatus.INSUFFICIENT, exc.balance_total)
        await balance.credit(
            connection,
            recipient_id,
            amount,
            op_key=recipient_op_key,
            reason="give_received",
            ref=f"from:{sender_id}",
        )
        await connection.exec_driver_sql(
            "INSERT INTO user_give_daily (user_id, give_date, give_count) VALUES (%s, %s, 1) "
            "ON DUPLICATE KEY UPDATE give_count = give_count + 1",
            (sender_id, today),
        )
        return GiveOutcome(GiveStatus.GIVEN)

    return await balance.run_in_transaction(work)


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
        chat_id = update.effective_chat.id
        message_id = update.message.message_id
        outcome = await transfer_coins(
            sender_id,
            await find_recipient_id(target_name),
            amount,
            sender_op_key=give_op_key(chat_id, message_id),
            recipient_op_key=give_recipient_op_key(chat_id, message_id),
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
        elif fee > 0:
            await update.message.reply_text(
                f"成功赠送 {amount} 枚硬币给用户 {target_name}，手续费 {fee} 枚硬币。"
            )
        else:
            await update.message.reply_text(f"成功赠送 {amount} 枚硬币给用户 {target_name}。")
    except Exception:
        logger.exception("赠送硬币失败: sender_id=%s target=%s", sender_id, target_name)
        await update.message.reply_text("转账过程中出现错误，请稍后再试。")
