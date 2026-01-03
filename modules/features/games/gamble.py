import asyncio
import random
from core import mysql_connection, process_user

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes
from core.command_cooldown import cooldown

# 全局变量保存当前赌博局数据（同一时间只允许一局）
gamble_game = None
gamble_lock = asyncio.Lock()

@cooldown
async def gamble_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global gamble_game
    user_id = update.effective_user.id
    
    # 判断用户权限是否 >= 1
    if await process_user.get_user_permission(user_id) < 1:
        await update.message.reply_text("您的权限不足，无法使用赌博命令。")
        return

    async with gamble_lock:
        if gamble_game is not None:
            await update.message.reply_text("赌博已在进行中，请等待本局结束。")
            return

        keyboard = [
            [
                InlineKeyboardButton("押注 5 金币", callback_data="gamble_5"),
                InlineKeyboardButton("押注 10 金币", callback_data="gamble_10"),
                InlineKeyboardButton("押注 20 金币", callback_data="gamble_20")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        msg = await update.message.reply_text(
            "赌博开始！请点击下面按钮选择押注金额。\n\n当前参与者：暂无\n\n开奖时间：5分钟",
            reply_markup=reply_markup
        )
        # 初始化赌博局数据
        gamble_game = {
            "chat_id": update.effective_chat.id,
            "message_id": msg.message_id,
            "bets": {},
            "participants": {},
            "prize": 0,
            "reply_markup": reply_markup
        }
    # 5分钟后开奖
    context.application.create_task(gamble_finish(context))

async def gamble_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global gamble_game
    query = update.callback_query

    async with gamble_lock:
        if gamble_game is None:
            await query.answer("当前没有活动的赌博游戏", show_alert=True)
            return

        user_id = query.from_user.id
        username = query.from_user.username if query.from_user.username else query.from_user.first_name

        # 用户若已参与则提示
        if user_id in gamble_game["bets"]:
            await query.answer("您已参与，请等待开奖。", show_alert=True)
            return

    # 获取押注金额
    try:
        bet_value = int(query.data.split("_")[1])
    except Exception:
        await query.answer("数据错误", show_alert=True)
        return

    # 从数据库中检测用户硬币余额是否充足，并扣除押注硬币
    try:
        async with mysql_connection.transaction() as connection:
            result = await mysql_connection.fetch_one(
                "SELECT coins, coins_paid FROM user WHERE id = %s",
                (user_id,),
                connection=connection,
            )
            current_coins = (result[0] or 0) + (result[1] or 0) if result else 0
            if not result or current_coins < bet_value:
                await query.answer("您的硬币不足", show_alert=True)
                return
            spent = await process_user.spend_user_coins(
                user_id,
                bet_value,
                connection=connection,
            )
            if not spent:
                await query.answer("您的硬币不足", show_alert=True)
                return
    except Exception:
        await query.answer("扣除硬币时出错，请稍后再试。", show_alert=True)
        return

    async with gamble_lock:
        # 记录用户押注
        gamble_game["bets"][user_id] = bet_value
        gamble_game["participants"][user_id] = username
        gamble_game["prize"] += bet_value

        # 构建当前参与人员文本
        participants_text = ""
        for uid, bet in gamble_game["bets"].items():
            uname = gamble_game["participants"][uid]
            participants_text += f"@{uname} 押注 {bet} 金币\n"

        new_text = (
            "赌博开始！请点击下面按钮选择押注金额。\n\n当前参与者：\n" +
            (participants_text if participants_text else "暂无") +
            "\n\n开奖时间：5分钟"
        )
        try:
            await context.bot.edit_message_text(
                text=new_text,
                chat_id=gamble_game["chat_id"],
                message_id=gamble_game["message_id"],
                reply_markup=gamble_game["reply_markup"]
            )
        except Exception:
            pass

    await query.answer(f"成功押注 {bet_value} 金币，等待开奖", show_alert=True)

async def gamble_finish(context: ContextTypes.DEFAULT_TYPE):
    global gamble_game
    await asyncio.sleep(300)  # 等待5分钟开奖

    async with gamble_lock:
        if gamble_game is None:
            return

        chat_id = gamble_game["chat_id"]
        message_id = gamble_game["message_id"]

        if not gamble_game["bets"]:
            result_text = "本局赌博无人参与！"
        else:
            # 根据押注金额作为权重抽取中奖者
            users = list(gamble_game["bets"].keys())
            weights = [gamble_game["bets"][uid] for uid in users]
            winner_id = random.choices(users, weights=weights, k=1)[0]
            winner_name = gamble_game["participants"][winner_id]
            prize = gamble_game["prize"]

            # 将奖池金额加入中奖者账户
            await process_user.update_user_coins(winner_id, prize)

            # 构建参与详情文本
            participants_text = ""
            for uid, bet in gamble_game["bets"].items():
                uname = gamble_game["participants"][uid]
                participants_text += f"@{uname} 押注 {bet} 金币\n"

            result_text = (
                f"开奖时间到！\n"
                f"中奖者：@{winner_name}\n"
                f"获得奖池所有 {prize} 金币！\n\n"
                f"参与详情：\n{participants_text}"
            )

        try:
            await context.bot.edit_message_text(
                text=result_text,
                chat_id=chat_id,
                message_id=message_id
            )
        except Exception:
            pass

        # 清空当前赌博局数据
        gamble_game = None
