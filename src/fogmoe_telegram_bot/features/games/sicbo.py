import asyncio
import logging
import random
from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from fogmoe_telegram_bot.core import balance, process_user
from fogmoe_telegram_bot.core.command_cooldown import cooldown

# 设置日志
logger = logging.getLogger(__name__)

# 定义游戏状态字典和锁
active_games: dict[int, dict] = {}  # 储存活跃游戏: {user_id: game_state}
game_locks: dict[int, asyncio.Lock] = {}  # 用户游戏锁: {user_id: asyncio.Lock()}

# 可选的下注金额
BET_AMOUNTS = (1, 5, 10, 20, 50, 100)

# 骰宝赔率表
PAYOUT_RATES = {
    "big": 1, "small": 1, "odd": 1, "even": 1,
    "sum_4": 60, "sum_5": 30, "sum_6": 18, "sum_7": 12,
    "sum_8": 8, "sum_9": 6, "sum_10": 6, "sum_11": 6,
    "sum_12": 6, "sum_13": 8, "sum_14": 12, "sum_15": 18,
    "sum_16": 30, "sum_17": 60, "any_triple": 30,
    "triple_1": 180, "triple_2": 180, "triple_3": 180,
    "triple_4": 180, "triple_5": 180, "triple_6": 180,
}

# 下注类型转中文名称
BET_TYPE_NAMES = {
    "big": "大 (11-17)", "small": "小 (4-10)", "odd": "单 (奇数)", "even": "双 (偶数)",
    "sum_4": "总和4", "sum_5": "总和5", "sum_6": "总和6", "sum_7": "总和7",
    "sum_8": "总和8", "sum_9": "总和9", "sum_10": "总和10", "sum_11": "总和11",
    "sum_12": "总和12", "sum_13": "总和13", "sum_14": "总和14", "sum_15": "总和15",
    "sum_16": "总和16", "sum_17": "总和17", "any_triple": "任意围骰",
    "triple_1": "围骰1", "triple_2": "围骰2", "triple_3": "围骰3",
    "triple_4": "围骰4", "triple_5": "围骰5", "triple_6": "围骰6",
}

# 获取用户锁
def get_user_lock(user_id: int) -> asyncio.Lock:
    if user_id not in game_locks:
        game_locks[user_id] = asyncio.Lock()
    return game_locks[user_id]

class AlreadySettled(Exception):
    """这一局的下注已经结算过（同一个面板消息的 op_key 已经记账），本次没有改动任何数据。"""


def bet_op_key(chat_id: int, message_id: int) -> str:
    """一局骰宝对应一个面板消息，下注扣款以它为幂等键。"""
    return balance.make_op_key("sicbo", chat_id, message_id, "bet")


def win_op_key(chat_id: int, message_id: int) -> str:
    return balance.make_op_key("sicbo", chat_id, message_id, "win")


async def settle_bet(
    user_id: int, chat_id: int, message_id: int, bet_amount: int, winnings: int
) -> int:
    """扣下注、赢了就入账奖金，同一个事务：要么都生效，要么都不生效。返回结算后的总余额。

    余额不足抛 `balance.InsufficientBalance`；这一局已经结算过抛 `AlreadySettled`。
    """

    async def work(connection):
        result = await balance.debit(
            connection,
            user_id,
            bet_amount,
            op_key=bet_op_key(chat_id, message_id),
            reason="sicbo_bet",
        )
        if not result.applied:
            raise AlreadySettled()
        if winnings > 0:
            result = await balance.credit(
                connection,
                user_id,
                winnings,
                op_key=win_op_key(chat_id, message_id),
                reason="sicbo_win",
            )
        return result.balance_total

    return await balance.run_in_transaction(work)

# 定期清理过期游戏
async def cleanup_expired_games(context: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.now()
    expired_users = []
    for user_id, game_data in list(active_games.items()):
        if "start_time" not in game_data:
            game_data["start_time"] = now
        elif (now - game_data["start_time"]) > timedelta(minutes=10):
            expired_users.append(user_id)
    for user_id in expired_users:
        if user_id in active_games:
            del active_games[user_id]
            logger.info(f"已清理用户 {user_id} 的过期游戏会话")
    for user_id in list(game_locks.keys()):
        if user_id not in active_games:
            del game_locks[user_id]

# 创建下注类型选择键盘
def get_bet_type_keyboard(user_id: int) -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("大 (11-17)", callback_data=f"sicbo_{user_id}_bet_big"),
         InlineKeyboardButton("小 (4-10)", callback_data=f"sicbo_{user_id}_bet_small")],
        [InlineKeyboardButton("单 (奇数)", callback_data=f"sicbo_{user_id}_bet_odd"),
         InlineKeyboardButton("双 (偶数)", callback_data=f"sicbo_{user_id}_bet_even")],
        [InlineKeyboardButton("总和 (4-10)", callback_data=f"sicbo_{user_id}_sum_low"),
         InlineKeyboardButton("总和 (11-17)", callback_data=f"sicbo_{user_id}_sum_high")],
        [InlineKeyboardButton("任意围骰", callback_data=f"sicbo_{user_id}_bet_any_triple"),
         InlineKeyboardButton("特定围骰", callback_data=f"sicbo_{user_id}_specific_triples")],
        [InlineKeyboardButton("❌ 取消", callback_data=f"sicbo_{user_id}_cancel")]
    ]
    return InlineKeyboardMarkup(keyboard)

# 创建总和低值选择键盘
def get_sum_low_keyboard(user_id: int) -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("总和4 (赔率60:1)", callback_data=f"sicbo_{user_id}_bet_sum_4"),
         InlineKeyboardButton("总和5 (赔率30:1)", callback_data=f"sicbo_{user_id}_bet_sum_5")],
        [InlineKeyboardButton("总和6 (赔率18:1)", callback_data=f"sicbo_{user_id}_bet_sum_6"),
         InlineKeyboardButton("总和7 (赔率12:1)", callback_data=f"sicbo_{user_id}_bet_sum_7")],
        [InlineKeyboardButton("总和8 (赔率8:1)", callback_data=f"sicbo_{user_id}_bet_sum_8"),
         InlineKeyboardButton("总和9 (赔率6:1)", callback_data=f"sicbo_{user_id}_bet_sum_9")],
        [InlineKeyboardButton("总和10 (赔率6:1)", callback_data=f"sicbo_{user_id}_bet_sum_10")],
        [InlineKeyboardButton("⬅️ 返回", callback_data=f"sicbo_{user_id}_back_to_main"),
         InlineKeyboardButton("❌ 取消", callback_data=f"sicbo_{user_id}_cancel")]
    ]
    return InlineKeyboardMarkup(keyboard)

# 创建总和高值选择键盘
def get_sum_high_keyboard(user_id: int) -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("总和11 (赔率6:1)", callback_data=f"sicbo_{user_id}_bet_sum_11"),
         InlineKeyboardButton("总和12 (赔率6:1)", callback_data=f"sicbo_{user_id}_bet_sum_12")],
        [InlineKeyboardButton("总和13 (赔率8:1)", callback_data=f"sicbo_{user_id}_bet_sum_13"),
         InlineKeyboardButton("总和14 (赔率12:1)", callback_data=f"sicbo_{user_id}_bet_sum_14")],
        [InlineKeyboardButton("总和15 (赔率18:1)", callback_data=f"sicbo_{user_id}_bet_sum_15"),
         InlineKeyboardButton("总和16 (赔率30:1)", callback_data=f"sicbo_{user_id}_bet_sum_16")],
        [InlineKeyboardButton("总和17 (赔率60:1)", callback_data=f"sicbo_{user_id}_bet_sum_17")],
        [InlineKeyboardButton("⬅️ 返回", callback_data=f"sicbo_{user_id}_back_to_main"),
         InlineKeyboardButton("❌ 取消", callback_data=f"sicbo_{user_id}_cancel")]
    ]
    return InlineKeyboardMarkup(keyboard)

# 创建特定围骰选择键盘
def get_specific_triples_keyboard(user_id: int) -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("围骰1 (赔率180:1)", callback_data=f"sicbo_{user_id}_bet_triple_1"),
         InlineKeyboardButton("围骰2 (赔率180:1)", callback_data=f"sicbo_{user_id}_bet_triple_2")],
        [InlineKeyboardButton("围骰3 (赔率180:1)", callback_data=f"sicbo_{user_id}_bet_triple_3"),
         InlineKeyboardButton("围骰4 (赔率180:1)", callback_data=f"sicbo_{user_id}_bet_triple_4")],
        [InlineKeyboardButton("围骰5 (赔率180:1)", callback_data=f"sicbo_{user_id}_bet_triple_5"),
         InlineKeyboardButton("围骰6 (赔率180:1)", callback_data=f"sicbo_{user_id}_bet_triple_6")],
        [InlineKeyboardButton("⬅️ 返回", callback_data=f"sicbo_{user_id}_back_to_main"),
         InlineKeyboardButton("❌ 取消", callback_data=f"sicbo_{user_id}_cancel")]
    ]
    return InlineKeyboardMarkup(keyboard)

# 创建下注金额选择键盘
def get_bet_amount_keyboard(user_id: int) -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("1 金币", callback_data=f"sicbo_{user_id}_amount_1"),
         InlineKeyboardButton("5 金币", callback_data=f"sicbo_{user_id}_amount_5"),
         InlineKeyboardButton("10 金币", callback_data=f"sicbo_{user_id}_amount_10")],
        [InlineKeyboardButton("20 金币", callback_data=f"sicbo_{user_id}_amount_20"),
         InlineKeyboardButton("50 金币", callback_data=f"sicbo_{user_id}_amount_50"),
         InlineKeyboardButton("100 金币", callback_data=f"sicbo_{user_id}_amount_100")],
        [InlineKeyboardButton("❌ 取消", callback_data=f"sicbo_{user_id}_cancel")]
    ]
    return InlineKeyboardMarkup(keyboard)

# 掷骰子并计算结果
def roll_dice() -> tuple[list[int], dict]:
    dice = [random.randint(1, 6) for _ in range(3)]
    total = sum(dice)
    results = {
        "dice": dice, "total": total,
        "big": 11 <= total <= 17 and (dice[0] != dice[1] or dice[1] != dice[2] or dice[0] != dice[2]),
        "small": 4 <= total <= 10 and (dice[0] != dice[1] or dice[1] != dice[2] or dice[0] != dice[2]),
        "odd": total % 2 == 1, "even": total % 2 == 0,
        "any_triple": dice[0] == dice[1] == dice[2],
        "triple_1": dice[0] == dice[1] == dice[2] == 1,
        "triple_2": dice[0] == dice[1] == dice[2] == 2,
        "triple_3": dice[0] == dice[1] == dice[2] == 3,
        "triple_4": dice[0] == dice[1] == dice[2] == 4,
        "triple_5": dice[0] == dice[1] == dice[2] == 5,
        "triple_6": dice[0] == dice[1] == dice[2] == 6,
    }
    for i in range(4, 18):
        results[f"sum_{i}"] = (total == i)
    if results["any_triple"]:
        results["big"] = False
        results["small"] = False
    return dice, results

# 获取骰子表情
def get_dice_emoji(dice_value: int) -> str:
    return {1: "⚀", 2: "⚁", 3: "⚂", 4: "⚃", 5: "⚄", 6: "⚅"}.get(dice_value, "🎲")

# 结束游戏并清理资源
def end_game(user_id: int) -> None:
    if user_id in active_games:
        del active_games[user_id]
    if user_id in game_locks:
        del game_locks[user_id]

@cooldown
async def sicbo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user_lock = get_user_lock(user_id)

    try:
        if not user_lock.locked():
            async with user_lock:
                if user_id in active_games:
                    await update.message.reply_text("您已经在一个骰宝游戏中，请先完成当前游戏。")
                    return
                if not await process_user.async_user_exists(user_id):
                    await update.message.reply_text("请先使用 /me 命令注册后再开始游戏。")
                    return
                user_coins = await process_user.async_get_user_coins(user_id)
                if user_coins < 1:
                    await update.message.reply_text("您的金币不足，至少需要1枚金币才能开始游戏。")
                    return
                active_games[user_id] = {
                    "bet_type": None,
                    "bet_amount": 0,
                    "message_id": None,
                    "start_time": datetime.now()
                }
            welcome_message = (
                "🎲 *骰宝游戏* 🎲\n\n"
                "欢迎来到骰宝！游戏规则：\n"
                "- 三个骰子的点数总和决定输赢\n"
                "- 大：总和为11-17（赔率1:1）\n"
                "- 小：总和为4-10（赔率1:1）\n"
                "- 单：总和为奇数（赔率1:1）\n"
                "- 双：总和为偶数（赔率1:1）\n"
                "- 总和：压中特定点数和（赔率不同）\n"
                "- 围骰：三个骰子点数相同（高赔率）\n\n"
                "注意：如果出现围骰，大小玩法都算输\n\n"
                "请选择您的下注类型："
            )
            message = await update.message.reply_text(welcome_message, reply_markup=get_bet_type_keyboard(user_id), parse_mode="Markdown")
            active_games[user_id]["message_id"] = message.message_id
        else:
            await update.message.reply_text("操作太快，请稍后再试。")
    except Exception as e:
        logger.error(f"启动骰宝游戏时出错: {str(e)}")
        if user_id in active_games:
            del active_games[user_id]
        await update.message.reply_text("启动游戏时出现错误，请稍后再试。")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    callback_data = query.data
    user_id = query.from_user.id

    if not callback_data.startswith("sicbo_"):
        await query.answer("无效的操作")
        return

    query_parts = callback_data.split('_')
    game_user_id = int(query_parts[1])
    action = '_'.join(query_parts[2:])

    if game_user_id != user_id:
        await query.answer("这不是您的游戏，请使用 /sicbo 开始自己的游戏", show_alert=True)
        return

    if user_id not in active_games:
        await query.answer("游戏已结束或已被取消")
        await query.edit_message_text("游戏已结束或已被取消。请使用 /sicbo 开始新游戏。")
        return

    # 面板必须是这局游戏发出的那一条：旧面板的按钮不会作用到当前这一局，
    # 下注扣款的 op_key 也由这条消息的位置决定。
    stored_message_id = active_games[user_id].get("message_id")
    if stored_message_id is not None and stored_message_id != query.message.message_id:
        await query.answer("这个面板已失效，请使用 /sicbo 开始新游戏", show_alert=True)
        return

    user_lock = get_user_lock(user_id)
    if not user_lock.locked():
        async with user_lock:
            if action == "cancel":
                end_game(user_id)
                await query.answer("游戏已取消")
                await query.edit_message_text("骰宝游戏已取消。")
                return
            elif action == "back_to_main":
                await query.answer()
                await query.edit_message_text(
                    "🎲 *骰宝游戏* 🎲\n\n请选择您的下注类型：",
                    reply_markup=get_bet_type_keyboard(user_id),
                    parse_mode="Markdown"
                )
            elif action in ["sum_low", "sum_high", "specific_triples"]:
                if action == "sum_low":
                    keyboard = get_sum_low_keyboard(user_id)
                    text = "请选择要下注的总和点数 (4-10)："
                elif action == "sum_high":
                    keyboard = get_sum_high_keyboard(user_id)
                    text = "请选择要下注的总和点数 (11-17)："
                else:
                    keyboard = get_specific_triples_keyboard(user_id)
                    text = "请选择要下注的特定围骰："
                await query.edit_message_text(text, reply_markup=keyboard)
            elif action.startswith("bet_"):
                bet_type = action.replace("bet_", "")
                active_games[user_id]["bet_type"] = bet_type
                bet_name = BET_TYPE_NAMES.get(bet_type, bet_type)
                payout_rate = PAYOUT_RATES.get(bet_type, 1)
                await query.edit_message_text(
                    f"您选择了: *{bet_name}* (赔率 {payout_rate}:1)\n\n请选择您要下注的金币数量：",
                    reply_markup=get_bet_amount_keyboard(user_id),
                    parse_mode="Markdown"
                )
            elif action.startswith("amount_"):
                try:
                    bet_amount = int(action.replace("amount_", ""))
                    bet_type = active_games[user_id]["bet_type"]
                    if bet_amount not in BET_AMOUNTS or bet_type not in PAYOUT_RATES:
                        await query.answer("请先选择下注类型", show_alert=True)
                        return
                    active_games[user_id]["bet_amount"] = bet_amount
                    bet_name = BET_TYPE_NAMES.get(bet_type, bet_type)
                    dice, results = roll_dice()
                    dice_emojis = [get_dice_emoji(d) for d in dice]
                    dice_display = " ".join(dice_emojis)
                    win = results.get(bet_type, False)
                    payout_rate = PAYOUT_RATES.get(bet_type, 1)
                    winnings = bet_amount * (1 + payout_rate) if win else 0
                    # 扣下注与入账奖金在同一个事务里，余额不足时什么都不会发生。
                    try:
                        new_balance = await settle_bet(
                            user_id,
                            query.message.chat.id,
                            query.message.message_id,
                            bet_amount,
                            winnings,
                        )
                    except balance.InsufficientBalance as exc:
                        await query.edit_message_text(
                            f"您的金币不足！您只有 {exc.balance_total} 金币。\n请使用 /sicbo 重新开始游戏并选择较小的下注金额。"
                        )
                        end_game(user_id)
                        return
                    except balance.UserNotFound:
                        await query.edit_message_text("请先使用 /me 命令注册后再开始游戏。")
                        end_game(user_id)
                        return
                    except AlreadySettled:
                        await query.edit_message_text("这一局已经结算过了。请使用 /sicbo 开始新游戏。")
                        end_game(user_id)
                        return
                    dice_sum = results["total"]
                    result_description = []
                    if dice_sum >= 11 and dice_sum <= 17 and not results["any_triple"]:
                        result_description.append("大")
                    elif dice_sum >= 4 and dice_sum <= 10 and not results["any_triple"]:
                        result_description.append("小")
                    if dice_sum % 2 == 1:
                        result_description.append("单")
                    else:
                        result_description.append("双")
                    if results["any_triple"]:
                        result_description.append(f"围骰{dice[0]}")
                    result_text = "、".join(result_description)
                    result_message = (
                        f"🎲 *骰宝游戏结果* 🎲\n\n"
                        f"骰子点数: {dice_display} = {dice_sum}\n"
                        f"结果特性: {result_text}\n\n"
                        f"您下注: *{bet_name}* {bet_amount} 金币\n"
                        f"{'恭喜您赢了! 🎉' if win else '很遗憾，您输了! 😔'}\n"
                    )

                    if win:
                        result_message += f"赔率: {payout_rate}:1\n获得: {winnings} 金币"
                    else:
                        result_message += f"您损失了 {bet_amount} 金币"

                    result_message += f"\n\n当前余额: {new_balance} 金币\n\n如需再玩一次，请使用 /sicbo 命令。"
                    await query.edit_message_text(result_message, parse_mode="Markdown")
                    end_game(user_id)
                except Exception as e:
                    logger.error(f"处理下注和结果时出错: {str(e)}")
                    await query.edit_message_text("游戏过程中出现错误，请稍后再试。")
                    end_game(user_id)
    else:
        await query.answer("请勿重复点击按钮", show_alert=True)

def setup_sicbo_handlers(application):
    application.add_handler(CommandHandler("sicbo", sicbo_command))
    application.add_handler(CallbackQueryHandler(handle_callback, pattern=r"^sicbo_"))
    application.job_queue.run_repeating(cleanup_expired_games, interval=300)
    logging.info("已加载骰宝游戏处理器")
