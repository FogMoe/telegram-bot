import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncConnection
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from core import balance, mysql_connection, process_user, stake_reward_pool
from core.command_cooldown import cooldown
from core.redaction import report_error

REWARD_INTERVAL_DAYS = 7
WITHDRAW_FEE_RATE = 0.03
MAX_DAILY_RATE = 0.3
MIN_DAILY_RATE = 0.05


async def get_total_coins(*, connection=None):
    row = await mysql_connection.fetch_one(
        "SELECT SUM(coins + coins_paid) FROM user", connection=connection
    )
    return row[0] if row and row[0] else 0


async def get_total_staked(*, connection=None):
    row = await mysql_connection.fetch_one(
        "SELECT SUM(stake_amount) FROM user_stakes", connection=connection
    )
    return row[0] if row and row[0] else 0


async def calculate_reward_rate(*, connection=None):
    total_coins = await get_total_coins(connection=connection)
    total_staked = await get_total_staked(connection=connection)

    if total_staked == 0 or total_coins == 0:
        return MAX_DAILY_RATE

    stake_ratio = min(1.0, float(total_staked) / (float(total_coins) + float(total_staked)))
    max_rate = MAX_DAILY_RATE
    min_rate = MIN_DAILY_RATE
    reward_rate = max_rate - stake_ratio * (max_rate - min_rate)

    return reward_rate


def _calculate_reward_for_intervals(
    stake_amount,
    reward_rate: float,
    intervals: int,
) -> int:
    if intervals <= 0:
        return 0

    reward_days = intervals * REWARD_INTERVAL_DAYS
    reward = (
        Decimal(str(stake_amount))
        * Decimal(str(reward_rate))
        * Decimal(reward_days)
        / Decimal("100")
    )
    return max(0, int(reward))


def _calculate_payable_intervals(
    stake_amount,
    reward_rate: float,
    intervals_passed: int,
    pool_balance,
) -> int:
    pool_balance = Decimal(str(pool_balance or 0))
    if intervals_passed <= 0 or pool_balance <= 0:
        return 0

    low = 0
    high = intervals_passed
    while low < high:
        mid = (low + high + 1) // 2
        reward = _calculate_reward_for_intervals(stake_amount, reward_rate, mid)
        if Decimal(reward) <= pool_balance:
            low = mid
        else:
            high = mid - 1

    if _calculate_reward_for_intervals(stake_amount, reward_rate, low) <= 0:
        return 0
    return low


def _calculate_reward_window(
    user_stake: dict,
    reward_rate: float,
    *,
    now: datetime | None = None,
) -> tuple[int, int, datetime]:
    last_reward_time = user_stake["last_reward_time"] or user_stake["stake_time"]
    now = now or datetime.now()
    elapsed_seconds = max(0, (now - last_reward_time).total_seconds())
    days_passed = int(elapsed_seconds // 86400)
    intervals_passed = days_passed // REWARD_INTERVAL_DAYS
    reward = _calculate_reward_for_intervals(
        user_stake["stake_amount"],
        reward_rate,
        intervals_passed,
    )
    return reward, intervals_passed, last_reward_time


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
    reward, _, _ = _calculate_reward_window(user_stake, reward_rate)
    return reward


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
    status_message += f"回报按天累计，每{REWARD_INTERVAL_DAYS}天可领取一次。\n"
    status_message += f"取出本金将收取 {int(WITHDRAW_FEE_RATE * 100)}% 手续费。\n"

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


# ---------------------------------------------------------------------------
# 质押操作：余额、奖池与质押记录在同一个事务里提交
#
# 加锁顺序固定为「先 user 行、后奖池行」（与对话计费等其他路径一致）：每个操作先
# `lock_user`，再读自己的质押记录，需要奖池时才锁奖池行。同一用户的质押、领奖、赎回因此
# 串行；不同用户只在奖池行上排队。死锁时 `run_in_transaction` 整个事务重跑，所以操作函数里
# 不做事务外的副作用，消息在事务提交之后由调用方发送。
# ---------------------------------------------------------------------------

_STAMP_FORMAT = "%Y%m%dT%H%M%S"


def stake_open_op_key(chat_id: int, message_id: int) -> str:
    """质押扣款：以 /stake 命令消息为身份，同一条命令被重复投递不会再扣一次。"""
    return balance.make_op_key("stake", chat_id, message_id)


def stake_collect_op_key(user_id: int, stake_time: datetime, window_start: datetime) -> str:
    """领奖：一次质押（user_id + stake_time）里从 `window_start` 起的那一段领奖窗口。

    领奖成功会把 last_reward_time 推进到窗口之后，所以同一个窗口只会领一次；
    奖励入账与奖池扣减共用这个 op_key（分属两张账本）。
    """
    return balance.make_op_key(
        "stake_collect",
        user_id,
        stake_time.strftime(_STAMP_FORMAT),
        window_start.strftime(_STAMP_FORMAT),
    )


def stake_withdraw_op_key(user_id: int, stake_time: datetime) -> str:
    """赎回本金：一次质押只能赎回一次（质押记录随赎回删除）。"""
    return balance.make_op_key("stake_withdraw", user_id, stake_time.strftime(_STAMP_FORMAT))


def stake_withdraw_reward_op_key(user_id: int, stake_time: datetime) -> str:
    """赎回时顺带结算的回报；入账与奖池扣减共用。"""
    return balance.make_op_key(
        "stake_withdraw_reward", user_id, stake_time.strftime(_STAMP_FORMAT)
    )


class OpenStatus(StrEnum):
    STAKED = "staked"
    REPLAYED = "replayed"  # 同一条命令被重复投递，上一次已经成功
    ALREADY_STAKED = "already_staked"
    INSUFFICIENT = "insufficient"
    NOT_REGISTERED = "not_registered"


@dataclass(frozen=True)
class OpenStakeOutcome:
    status: OpenStatus
    balance_total: int = 0  # 余额不足时的当前余额


async def open_stake(user_id: int, amount: int, *, op_key: str) -> OpenStakeOutcome:
    """质押 `amount` 枚金币：扣款与质押记录同事务。余额不足不改动任何数据。"""

    async def work(connection: AsyncConnection) -> OpenStakeOutcome:
        try:
            balances = await balance.lock_user(connection, user_id)
        except balance.UserNotFound:
            return OpenStakeOutcome(OpenStatus.NOT_REGISTERED)

        # 同一条命令的重放：上一次的扣款与质押记录一起提交了，直接确认结果。
        if await balance.get_operation(op_key, connection=connection) is not None:
            return OpenStakeOutcome(OpenStatus.REPLAYED)
        if balances.total < amount:
            return OpenStakeOutcome(OpenStatus.INSUFFICIENT, balances.total)
        if await get_user_stake(user_id, connection=connection):
            return OpenStakeOutcome(OpenStatus.ALREADY_STAKED)

        try:
            await balance.debit(connection, user_id, amount, op_key=op_key, reason="stake")
        except balance.InsufficientBalance as exc:
            return OpenStakeOutcome(OpenStatus.INSUFFICIENT, exc.balance_total)
        await connection.exec_driver_sql(
            "INSERT INTO user_stakes (user_id, stake_amount, stake_time) VALUES (%s, %s, %s)",
            (user_id, amount, datetime.now()),
        )
        return OpenStakeOutcome(OpenStatus.STAKED)

    return await balance.run_in_transaction(work)


class CollectStatus(StrEnum):
    COLLECTED = "collected"
    NO_STAKE = "no_stake"
    NOT_YET = "not_yet"  # 不满一个领奖周期
    TOO_SMALL = "too_small"  # 满了周期但累计回报不足 1 金币
    POOL_EMPTY = "pool_empty"


@dataclass(frozen=True)
class CollectOutcome:
    status: CollectStatus
    reward: int = 0
    stake_amount: int = 0


async def collect_stake_reward(user_id: int) -> CollectOutcome:
    """领取回报：用户入账、奖池扣减与 last_reward_time 推进同事务。"""

    async def work(connection: AsyncConnection) -> CollectOutcome:
        try:
            await balance.lock_user(connection, user_id)
        except balance.UserNotFound:
            return CollectOutcome(CollectStatus.NO_STAKE)
        user_stake = await get_user_stake(user_id, connection=connection)
        if not user_stake:
            return CollectOutcome(CollectStatus.NO_STAKE)

        reward_rate = await calculate_reward_rate(connection=connection)
        reward_due, intervals_passed, last_reward_time = _calculate_reward_window(
            user_stake,
            reward_rate,
        )
        if intervals_passed <= 0:
            return CollectOutcome(CollectStatus.NOT_YET)
        if reward_due <= 0:
            return CollectOutcome(CollectStatus.TOO_SMALL)

        # 用户行已经锁住，现在才锁奖池行；按锁内读到的余额决定能发多少，扣减不会失败。
        pool_balance = await stake_reward_pool.get_pool_balance(
            connection=connection,
            for_update=True,
        )
        intervals_paid = _calculate_payable_intervals(
            user_stake["stake_amount"],
            reward_rate,
            intervals_passed,
            pool_balance,
        )
        reward = _calculate_reward_for_intervals(
            user_stake["stake_amount"],
            reward_rate,
            intervals_paid,
        )
        if intervals_paid <= 0 or reward <= 0:
            return CollectOutcome(CollectStatus.POOL_EMPTY)

        op_key = stake_collect_op_key(user_id, user_stake["stake_time"], last_reward_time)
        await balance.credit(
            connection, user_id, reward, op_key=op_key, reason="stake_reward"
        )
        await stake_reward_pool.debit_pool(
            connection, reward, op_key=op_key, reason="stake_reward", ref=op_key
        )
        await connection.exec_driver_sql(
            "UPDATE user_stakes SET last_reward_time = %s WHERE user_id = %s",
            (
                last_reward_time + timedelta(days=intervals_paid * REWARD_INTERVAL_DAYS),
                user_id,
            ),
        )
        return CollectOutcome(
            CollectStatus.COLLECTED,
            reward=reward,
            stake_amount=user_stake["stake_amount"],
        )

    return await balance.run_in_transaction(work)


class WithdrawStatus(StrEnum):
    WITHDRAWN = "withdrawn"
    NO_STAKE = "no_stake"


@dataclass(frozen=True)
class WithdrawOutcome:
    status: WithdrawStatus
    fee: int = 0
    principal: int = 0
    reward: int = 0
    reward_due: int = 0
    intervals_passed: int = 0


async def withdraw_stake_principal(user_id: int) -> WithdrawOutcome:
    """取出本金（扣 3% 手续费）并结算已满周期的回报：入账、奖池扣减、删除质押记录同事务。"""

    async def work(connection: AsyncConnection) -> WithdrawOutcome:
        try:
            await balance.lock_user(connection, user_id)
        except balance.UserNotFound:
            return WithdrawOutcome(WithdrawStatus.NO_STAKE)
        user_stake = await get_user_stake(user_id, connection=connection)
        if not user_stake:
            return WithdrawOutcome(WithdrawStatus.NO_STAKE)

        stake_amount = user_stake["stake_amount"]
        fee = int(stake_amount * WITHDRAW_FEE_RATE)
        refunded_principal = max(stake_amount - fee, 0)
        reward_rate = await calculate_reward_rate(connection=connection)
        reward_due, intervals_passed, _ = _calculate_reward_window(
            user_stake,
            reward_rate,
        )
        reward = 0
        if reward_due > 0 and intervals_passed > 0:
            pool_balance = await stake_reward_pool.get_pool_balance(
                connection=connection,
                for_update=True,
            )
            intervals_paid = _calculate_payable_intervals(
                user_stake["stake_amount"],
                reward_rate,
                intervals_passed,
                pool_balance,
            )
            reward = _calculate_reward_for_intervals(
                user_stake["stake_amount"],
                reward_rate,
                intervals_paid,
            )

        stake_time = user_stake["stake_time"]
        if refunded_principal > 0:
            await balance.credit(
                connection,
                user_id,
                refunded_principal,
                op_key=stake_withdraw_op_key(user_id, stake_time),
                reason="stake_withdraw",
            )
        if reward > 0:
            reward_key = stake_withdraw_reward_op_key(user_id, stake_time)
            await balance.credit(
                connection, user_id, reward, op_key=reward_key, reason="stake_reward"
            )
            await stake_reward_pool.debit_pool(
                connection, reward, op_key=reward_key, reason="stake_reward", ref=reward_key
            )

        await connection.exec_driver_sql(
            "DELETE FROM user_stakes WHERE user_id = %s",
            (user_id,),
        )
        return WithdrawOutcome(
            WithdrawStatus.WITHDRAWN,
            fee=fee,
            principal=refunded_principal,
            reward=reward,
            reward_due=reward_due,
            intervals_passed=intervals_passed,
        )

    return await balance.run_in_transaction(work)


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


# ---------------------------------------------------------------------------
# Telegram 处理器：只做输入映射与回复
# ---------------------------------------------------------------------------


async def stake_coins(update: Update, context: ContextTypes.DEFAULT_TYPE, amount: int):
    user_id = update.effective_user.id
    message = update.message

    try:
        outcome = await open_stake(
            user_id,
            amount,
            op_key=stake_open_op_key(update.effective_chat.id, message.message_id),
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

        reward_rate = await calculate_reward_rate()
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
        outcome = await collect_stake_reward(user_id)
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

        reward_rate = await calculate_reward_rate()
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
        outcome = await withdraw_stake_principal(user_id)
        if outcome.status is WithdrawStatus.NO_STAKE:
            await query.answer("您没有质押任何金币。", show_alert=True)
            return

        msg = withdraw_message(outcome)
        reward_rate = await calculate_reward_rate()
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
