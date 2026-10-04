import asyncio
import random
from dataclasses import dataclass
from core import balance, mysql_connection
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
import logging
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes
from datetime import date
import time
from core.command_cooldown import cooldown
from core.redaction import log_exception, report_error


logger = logging.getLogger(__name__)
# 进程内的购买锁：保护下面的保底计数（进程内存状态）。余额与业务状态的一致性由数据库事务保证。
lock = asyncio.Lock()

# 添加用户刮刮乐记录字典，用于实现保底机制
# 格式: {user_id: {'count': 连续小于10金币次数, 'date': 最后抽取日期}}
scratch_records = {}

# 添加用户欢乐彩记录字典，用于实现保底机制
# 格式: {user_id: {'count': 连续0金币次数, 'date': 最后抽取日期}}
huanle_records = {}

# 添加用户最后抽奖消息记录
# 格式: {(user_id, chat_id): {'message_id': 消息ID, 'timestamp': 最后发送时间, 'message_type': '消息类型'}}
last_lottery_messages = {}

# 设置消息更新阈值（秒）- 超过这个时间才会发送新消息
MESSAGE_UPDATE_THRESHOLD = 30

MEMORY_LIMIT_PRICE = 100
# 权限等级 -> 升级到该等级的价格
PERMISSION_UPGRADE_PRICES = {1: 50, 2: 100, 3: 10000}
UPGRADE_CALLBACKS = {
    "shop_upgrade_1": 1,
    "shop_upgrade_2": 2,
    "shop_upgrade_3": 3,
}

SCRATCH_PRICE = 10
SCRATCH_PITY_THRESHOLD = 5
SCRATCH_PITY_BONUS = 10
HUANLE_PRICE = 1
HUANLE_PITY_THRESHOLD = 5
HUANLE_PITY_BONUS = 2

NOT_REGISTERED_MESSAGE = "请先使用 /me 命令获取个人信息。"
INSUFFICIENT_MESSAGE = "硬币不足，无法购买此商品。"


def shop_op_key(item: str, query_id: object) -> str:
    """商店购买的 op_key：以按钮回调的 query id 为身份，同一次点击被重复投递不会再扣一次。

    没有 query id（不应该发生）时退回一次性 op_key，此时没有重放保护。
    """
    if not query_id:
        return balance.new_op_key(f"shop:{item}")
    return balance.make_op_key("shop", item, query_id)


def permission_upgrade_refusal(current_permission: int, target_level: int) -> str | None:
    """当前权限不允许升级到 `target_level` 时返回提示文案，允许返回 None。"""
    if target_level == 1:
        if current_permission != 0:
            return "您已经拥有权限或已升级。"
    elif target_level == 2:
        if current_permission == 0:
            return "您需要先升级到1级权限。"
        if current_permission >= 2:
            return "您已经拥有2级或更高权限。"
    elif target_level == 3:
        if current_permission < 2:
            return "您需要先升级到2级权限。"
        if current_permission >= 3:
            return "您已经拥有3级或更高权限。"
    return None


def draw_scratch_reward(rng: random.Random | None = None) -> int:
    """刮刮乐：0～20 金币均匀分布。"""
    return (rng or random).randint(0, 20)


def draw_huanle_reward(rng: random.Random | None = None) -> int:
    """欢乐彩：0 金币 80%，1 金币 19%，5 金币 0.95%，100 金币 0.05%。"""
    p = (rng or random).random()
    if p < 0.80:
        return 0
    if p < 0.80 + 0.19:
        return 1
    if p < 0.80 + 0.19 + 0.0095:
        return 5
    return 100


def advance_pity(
    record: dict | None,
    *,
    today: date,
    miss: bool,
    threshold: int,
) -> tuple[dict, bool]:
    """保底计数前进一步，返回 (新的记录, 本次是否触发保底奖励)。

    连续「没中」达到 `threshold` 次（同一天内累计，隔天从头算）触发一次保底，随后计数清零。
    纯函数：只有购买事务提交之后才把新记录写回进程内的字典。
    """
    count = record["count"] if record and record["date"] == today else 0
    count = count + 1 if miss else 0
    if count >= threshold:
        return {"count": 0, "date": today}, True
    return {"count": count, "date": today}, False


@dataclass(frozen=True)
class LotteryPurchase:
    """一次购彩的结果。`ok` 为 False 时 `message` 是要给用户看的拒绝原因。"""

    ok: bool
    message: str = ""
    reward: int = 0
    bonus: int = 0
    pity: dict | None = None  # 事务提交后要写回的保底记录；重放时为 None


@cooldown
async def shop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /shop 命令：发送商城一级菜单
    """
    keyboard = [
        [InlineKeyboardButton("购买权限", callback_data="shop_buy_permission")],
        [InlineKeyboardButton("购买记忆上限 +1 - 100金币", callback_data="shop_buy_memory_limit")],
        [InlineKeyboardButton("购买彩票", callback_data="shop_buy_lottery")],
        [InlineKeyboardButton("关闭商店", callback_data="shop_close")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("欢迎来到商城，请选择购买项目：", reply_markup=reply_markup)


# ---------------------------------------------------------------------------
# 购买：扣款与业务状态在同一个事务里提交，任何一步失败都整体回滚（不需要退款）
# ---------------------------------------------------------------------------


async def buy_memory_limit(user_id: int, op_key: str) -> str:
    """购买永久记忆上限 +1，返回要给用户看的文案。"""

    async def work(connection) -> str:
        try:
            balances = await balance.lock_user(connection, user_id)
        except balance.UserNotFound:
            return NOT_REGISTERED_MESSAGE
        if balances.total < MEMORY_LIMIT_PRICE:
            return INSUFFICIENT_MESSAGE
        try:
            result = await balance.debit(
                connection,
                user_id,
                MEMORY_LIMIT_PRICE,
                op_key=op_key,
                reason="shop_memory",
            )
        except balance.InsufficientBalance:
            return INSUFFICIENT_MESSAGE
        if result.applied:
            await connection.exec_driver_sql(
                "UPDATE user SET permanent_records_limit = permanent_records_limit + 1 "
                "WHERE id = %s",
                (user_id,),
            )
        row = await mysql_connection.fetch_one(
            "SELECT permanent_records_limit FROM user WHERE id = %s",
            (user_id,),
            connection=connection,
        )
        new_limit = row[0] if row else "?"
        return f"购买成功！永久记忆上限已提升至 {new_limit} 条。"

    return await balance.run_in_transaction(work)


async def upgrade_permission(user_id: int, target_level: int, op_key: str) -> str:
    """购买权限升级到 `target_level` 级，返回要给用户看的文案。"""
    price = PERMISSION_UPGRADE_PRICES[target_level]

    async def work(connection) -> str:
        try:
            balances = await balance.lock_user(connection, user_id)
        except balance.UserNotFound:
            return NOT_REGISTERED_MESSAGE
        # 用户行已锁住，这是事务里第一次一致性读，看到的是上一个持锁者提交之后的权限。
        row = await mysql_connection.fetch_one(
            "SELECT permission FROM user WHERE id = %s",
            (user_id,),
            connection=connection,
        )
        current_permission = (row[0] if row else 0) or 0
        refusal = permission_upgrade_refusal(current_permission, target_level)
        if refusal:
            return refusal
        if balances.total < price:
            return INSUFFICIENT_MESSAGE
        try:
            result = await balance.debit(
                connection,
                user_id,
                price,
                op_key=op_key,
                reason="shop_permission",
            )
        except balance.InsufficientBalance:
            return INSUFFICIENT_MESSAGE
        if result.applied:
            await connection.exec_driver_sql(
                "UPDATE user SET permission = %s WHERE id = %s",
                (target_level, user_id),
            )
        return f"购买成功！您的权限已升级到{target_level}级。"

    return await balance.run_in_transaction(work)


async def _recorded_credit(connection, op_key: str) -> int:
    existing = await balance.get_operation(op_key, connection=connection)
    return existing.amount if existing else 0


async def _buy_lottery_ticket(
    user_id: int,
    op_key: str,
    *,
    item: str,
    price: int,
    draw_reward,
    pity_records: dict,
    pity_threshold: int,
    pity_bonus: int,
    is_miss,
    today: date,
) -> LotteryPurchase:
    """购买一张彩票：扣款、开奖入账、保底奖励在同一个事务里。

    保底计数是进程内状态，只在事务提交后才由调用方写回，所以回滚不会留下半个计数。
    同一次点击被重复投递（op_key 的扣款是重放）时，奖励已经随第一次事务提交，
    这里只读回当时的结果，不再开奖。
    """

    async def work(connection) -> LotteryPurchase:
        try:
            balances = await balance.lock_user(connection, user_id)
        except balance.UserNotFound:
            return LotteryPurchase(False, NOT_REGISTERED_MESSAGE)
        if balances.total < price:
            return LotteryPurchase(False, f"硬币不足，您当前只有 {balances.total} 个硬币。")

        reward = draw_reward()
        try:
            debit = await balance.debit(
                connection, user_id, price, op_key=op_key, reason=f"shop_{item}"
            )
        except balance.InsufficientBalance as exc:
            return LotteryPurchase(False, f"硬币不足，您当前只有 {exc.balance_total} 个硬币。")

        win_key = f"{op_key}:win"
        bonus_key = f"{op_key}:bonus"
        if not debit.applied:
            return LotteryPurchase(
                True,
                reward=await _recorded_credit(connection, win_key),
                bonus=await _recorded_credit(connection, bonus_key),
            )

        if reward > 0:
            await balance.credit(
                connection, user_id, reward, op_key=win_key, reason=f"shop_{item}_win"
            )
        pity, triggered = advance_pity(
            pity_records.get(user_id),
            today=today,
            miss=is_miss(reward),
            threshold=pity_threshold,
        )
        bonus = 0
        if triggered:
            bonus = pity_bonus
            await balance.credit(
                connection, user_id, bonus, op_key=bonus_key, reason=f"shop_{item}_bonus"
            )
        return LotteryPurchase(True, reward=reward, bonus=bonus, pity=pity)

    purchase = await balance.run_in_transaction(work)
    if purchase.ok and purchase.pity is not None:
        pity_records[user_id] = purchase.pity
    return purchase


async def buy_scratch_ticket(user_id: int, op_key: str, *, today: date | None = None):
    return await _buy_lottery_ticket(
        user_id,
        op_key,
        item="scratch",
        price=SCRATCH_PRICE,
        draw_reward=draw_scratch_reward,
        pity_records=scratch_records,
        pity_threshold=SCRATCH_PITY_THRESHOLD,
        pity_bonus=SCRATCH_PITY_BONUS,
        is_miss=lambda reward: reward < 10,
        today=today or date.today(),
    )


async def buy_huanle_ticket(user_id: int, op_key: str, *, today: date | None = None):
    return await _buy_lottery_ticket(
        user_id,
        op_key,
        item="huanle",
        price=HUANLE_PRICE,
        draw_reward=draw_huanle_reward,
        pity_records=huanle_records,
        pity_threshold=HUANLE_PITY_THRESHOLD,
        pity_bonus=HUANLE_PITY_BONUS,
        is_miss=lambda reward: reward == 0,
        today=today or date.today(),
    )


async def _post_lottery_record(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    user_id: int,
    user_label: str,
    game_name: str,
    reward: int,
    bonus: int,
) -> None:
    """把这次购彩记进聊天里的「最近的彩票记录」。

    30 秒内合并编辑同一条消息；已满 6 行或编辑失败时新发一条。
    """
    line = f"{user_label}: {game_name} → {reward}金币"
    if bonus:
        line += f" (触发保底奖励{bonus}金币!)"
    fresh_text = f"📊 最近的彩票记录:\n{line}"

    current_time = time.time()
    message_key = (user_id, chat_id)

    async def send_fresh() -> None:
        sent_msg = await context.bot.send_message(chat_id=chat_id, text=fresh_text)
        last_lottery_messages[message_key] = {
            'message_id': sent_msg.message_id,
            'timestamp': current_time,
            'message_type': 'lottery',
            'text': fresh_text
        }

    previous = last_lottery_messages.get(message_key)
    if (
        previous
        and current_time - previous['timestamp'] < MESSAGE_UPDATE_THRESHOLD
        and previous['message_type'] == 'lottery'
    ):
        old_text = previous.get('text', '')
        if len(old_text.split('\n')) >= 6:
            await send_fresh()
            return
        try:
            new_text = old_text + f"\n{line}"
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=previous['message_id'],
                text=new_text
            )
            previous['text'] = new_text
            previous['timestamp'] = current_time
        except Exception as exc:
            logger.debug("编辑彩票记录消息失败，改为发送新消息: %s", exc)
            await send_fresh()
        return

    await send_fresh()


async def shop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    处理商城按钮回调：
    - 一级菜单：显示“购买权限”、“购买记忆上限”、“购买彩票”和“关闭商店”按钮。
    - “购买权限”按钮：进入二级菜单，显示升级权限选项及返回按钮。
    - “购买彩票”按钮：进入二级菜单，显示“购买刮刮乐 - 10金币”、“购买欢乐彩 - 1金币”和“返回”按钮。
    - “购买刮刮乐 - 10金币”按钮：执行刮刮乐购买逻辑。
    - “购买欢乐彩 - 1金币”按钮：执行欢乐彩购买逻辑。
    - “返回”按钮：返回到一级菜单。
    - “关闭商店”按钮：删除商城消息。
    """
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = update.effective_chat.id

    if query.data == "shop_buy_permission":
        # 进入购买权限二级菜单
        keyboard = [
            [InlineKeyboardButton("升级权限等级到1级 - 50金币", callback_data="shop_upgrade_1")],
            [InlineKeyboardButton("升级权限等级到2级 - 100金币", callback_data="shop_upgrade_2")],
            [InlineKeyboardButton("升级权限等级到3级 - 10000金币", callback_data="shop_upgrade_3")],
            [InlineKeyboardButton("返回", callback_data="shop_home")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        try:
            await query.edit_message_text("请选择购买的项目：", reply_markup=reply_markup)
        except Exception as exc:
            logger.debug("刷新商店菜单失败: %s", exc)

    elif query.data == "shop_buy_lottery":
        # 进入购买彩票二级菜单
        keyboard = [
            [InlineKeyboardButton("购买刮刮乐 - 10金币", callback_data="shop_scratch")],
            [InlineKeyboardButton("购买欢乐彩 - 1金币", callback_data="shop_huanle")],
            [InlineKeyboardButton("返回", callback_data="shop_home")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        try:
            await query.edit_message_text("请选择购彩项目：", reply_markup=reply_markup)
        except Exception as exc:
            logger.debug("刷新购彩菜单失败: %s", exc)

    elif query.data == "shop_buy_memory_limit":
        # 购买永久记忆上限 +1
        async with lock:
            try:
                message = await buy_memory_limit(user_id, shop_op_key("memory", query.id))
            except Exception:
                log_exception(logger, f"购买记忆上限失败: user_id={user_id}")
                await query.answer("购买出现错误，请稍后再试。", show_alert=True)
            else:
                await query.answer(message, show_alert=True)

    elif query.data == "shop_home":
        # 返回到一级菜单
        keyboard = [
            [InlineKeyboardButton("购买权限", callback_data="shop_buy_permission")],
            [InlineKeyboardButton("购买记忆上限 +1 - 100金币", callback_data="shop_buy_memory_limit")],
            [InlineKeyboardButton("购买彩票", callback_data="shop_buy_lottery")],
            [InlineKeyboardButton("关闭商店", callback_data="shop_close")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        try:
            await query.edit_message_text("欢迎来到商城，请选择购买项目：", reply_markup=reply_markup)
        except Exception as exc:
            logger.debug("返回商城主菜单失败: %s", exc)

    elif query.data == "shop_close":
        # 删除商城消息
        try:
            await query.delete_message()
        except Exception as exc:
            logger.debug("关闭商城消息失败: %s", exc)

    elif query.data in UPGRADE_CALLBACKS:
        # 执行购买升级权限的操作
        level = UPGRADE_CALLBACKS[query.data]
        async with lock:
            try:
                message = await upgrade_permission(
                    user_id, level, shop_op_key(f"perm{level}", query.id)
                )
            except Exception:
                log_exception(logger, f"购买权限升级失败: user_id={user_id} level={level}")
                await query.answer("购买出现错误，请稍后再试。", show_alert=True)
            else:
                await query.answer(message, show_alert=True)

    elif query.data == "shop_scratch":
        # 购买刮刮乐：扣除10金币，随机获得0～20金币
        async with lock:
            try:
                purchase = await buy_scratch_ticket(user_id, shop_op_key("scratch", query.id))
                if not purchase.ok:
                    await query.answer(purchase.message, show_alert=True)
                    return
                reward = purchase.reward
                bonus_message = ""
                if purchase.bonus:
                    bonus_message = "由于您连续5次都没抽到10个以上的金币，系统赠送您10个金币作为安慰！"

                # 弹出提示
                message = f"恭喜！您获得了 {reward} 个金币。"
                if bonus_message:
                    message += f"\n\n{bonus_message}"
                await query.answer(message, show_alert=True)

                # 发送通知消息到当前聊天（优化为可能更新现有消息）
                user_username = f"@{query.from_user.username}" if query.from_user.username else query.from_user.first_name
                await _post_lottery_record(
                    context,
                    chat_id=chat_id,
                    user_id=user_id,
                    user_label=user_username,
                    game_name="刮刮乐",
                    reward=reward,
                    bonus=purchase.bonus,
                )
            except Exception as e:
                notice = report_error(logger, "购买刮刮乐时出错", e)
                await query.answer(f"购买刮刮乐时出错，请稍后再试。\n{notice}", show_alert=True)

    elif query.data == "shop_huanle":
        # 购买欢乐彩：扣除1金币，根据概率获得奖励
        async with lock:
            try:
                purchase = await buy_huanle_ticket(user_id, shop_op_key("huanle", query.id))
                if not purchase.ok:
                    await query.answer(purchase.message, show_alert=True)
                    return
                reward = purchase.reward
                bonus_message = ""
                if purchase.bonus:
                    bonus_message = "由于您连续5次都没有获得奖励，系统赠送您2个金币作为安慰！"

                # 弹出提示
                message = f"恭喜！您获得了 {reward} 个金币。"
                if bonus_message:
                    message += f"\n\n{bonus_message}"
                await query.answer(message, show_alert=True)

                # 发送通知消息到当前聊天（优化为可能更新现有消息）
                user_username = f"@{query.from_user.username}" if query.from_user.username else query.from_user.first_name
                await _post_lottery_record(
                    context,
                    chat_id=chat_id,
                    user_id=user_id,
                    user_label=user_username,
                    game_name="欢乐彩",
                    reward=reward,
                    bonus=purchase.bonus,
                )
            except Exception:
                logger.exception("购买欢乐彩失败: user_id=%s", user_id)
                await query.answer("购买欢乐彩时出错，请稍后再试。", show_alert=True)

# 修改清理函数以适配JobQueue使用
async def cleanup_message_records_job(context: ContextTypes.DEFAULT_TYPE):
    """清理旧的消息记录，每小时运行一次"""
    current_time = time.time()
    # 删除超过1小时的记录
    expired_keys = [k for k, v in last_lottery_messages.items() 
                  if current_time - v['timestamp'] > 3600]
    for key in expired_keys:
        if key in last_lottery_messages:
            del last_lottery_messages[key]
    print(f"清理了{len(expired_keys)}条过期抽奖消息记录")

# 保留原始函数以保持兼容性
async def cleanup_message_records():
    """原始清理函数，现在直接调用一次清理作业"""
    await cleanup_message_records_job(None)


def setup_shop_handlers(application) -> None:
    """注册商店命令、回调与消息记录清理任务。"""

    application.add_handler(CommandHandler("shop", shop_command))
    application.add_handler(CallbackQueryHandler(shop_callback, pattern=r"^shop_"))
    application.job_queue.run_repeating(
        cleanup_message_records_job,
        interval=3600,
        first=10,
    )
