import asyncio
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CommandHandler, CallbackQueryHandler, ContextTypes
import random
from core import process_user
from core.command_cooldown import cooldown
import logging

# 游戏状态常量
WAITING_PLAYER = "waiting_player"
CHOOSING = "choosing"
GAME_OVER = "game_over"


logger = logging.getLogger(__name__)
# 选择常量
ROCK = "rock"
PAPER = "paper"
SCISSORS = "scissors"

# 全局变量
active_games = {}  # 活跃游戏
waiting_room = None  # 等待中的玩家
waiting_room_lock = asyncio.Lock()  # 等待房间锁
game_locks = {}  # 游戏锁
game_timeouts = {}  # 超时任务

# 游戏结果映射
RESULT_MAP = {
    (ROCK, ROCK): "平局", (ROCK, PAPER): "布胜", (ROCK, SCISSORS): "石头胜",
    (PAPER, ROCK): "布胜", (PAPER, PAPER): "平局", (PAPER, SCISSORS): "剪刀胜",
    (SCISSORS, ROCK): "石头胜", (SCISSORS, PAPER): "剪刀胜", (SCISSORS, SCISSORS): "平局"
}

# 表情映射
EMOJI_MAP = {ROCK: "👊", PAPER: "✋", SCISSORS: "✌️"}

# 创建选择按钮键盘，加入用户ID增强安全性
def get_choice_keyboard(user_id):
    keyboard = [
        [
            InlineKeyboardButton("石头 👊", callback_data=f"rps_choice_{ROCK}_{user_id}"),
            InlineKeyboardButton("剪刀 ✌️", callback_data=f"rps_choice_{SCISSORS}_{user_id}"),
            InlineKeyboardButton("布 ✋", callback_data=f"rps_choice_{PAPER}_{user_id}")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

# 等待游戏按钮键盘
def get_waiting_keyboard():
    keyboard = [
        [
            InlineKeyboardButton("加入游戏 (消耗1金币)", callback_data="rps_join"),
            InlineKeyboardButton("取消等待", callback_data="rps_cancel")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

@cooldown
async def rps_game_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """开始石头剪刀布游戏"""
    global waiting_room
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    chat_id = update.effective_chat.id

    # 检查用户状态
    if not await process_user.async_user_exists(user_id):
        await update.message.reply_text("请先使用 /me 命令注册后再游玩。")
        return
    user_coins = await process_user.async_get_user_coins(user_id)
    if user_coins < 1:
        await update.message.reply_text("您的金币不足，需要至少1枚金币才能开始游戏。")
        return
    if any(user_id in [game['player1']['id'], game.get('player2', {}).get('id')] for game in active_games.values()):
        await update.message.reply_text("您已经在一个游戏中，请先完成该游戏。")
        return

    async with waiting_room_lock:
        if waiting_room and waiting_room['player_id'] == user_id:
            await update.message.reply_text("您已经创建了一个游戏等待中，请等待其他玩家加入或取消当前游戏。")
            return

        # 如果有等待玩家，匹配并开始游戏
        if waiting_room:
            game_id = random.randint(10000, 99999)
            waiting_player_id = waiting_room['player_id']
            waiting_player_name = waiting_room['player_name']
            waiting_chat_id = waiting_room['chat_id']
            waiting_message_id = waiting_room['message_id']
            same_chat = (waiting_chat_id == chat_id)

            # 扣除金币
            await process_user.async_update_user_coins(user_id, -1)
            await process_user.async_update_user_coins(waiting_player_id, -1)

            # 创建游戏
            game_locks[game_id] = asyncio.Lock()
            active_games[game_id] = {
                'state': CHOOSING,
                'player1': {'id': waiting_player_id, 'name': waiting_player_name, 'chat_id': waiting_chat_id, 'message_id': waiting_message_id, 'choice': None},
                'player2': {'id': user_id, 'name': username, 'chat_id': chat_id, 'message_id': None, 'choice': None},
                'same_chat': same_chat
            }

            if same_chat:
                try:
                    await context.bot.edit_message_text(
                        chat_id=waiting_chat_id,
                        message_id=waiting_message_id,
                        text=f"🎮 石头剪刀布游戏开始！\n\n玩家1: @{waiting_player_name} (未选择)\n玩家2: @{username} (未选择)\n\n游戏规则: 每位玩家消耗1金币，获胜者获得2金币奖励。\n请双方查看私聊消息进行选择。",
                        reply_markup=None
                    )
                    active_games[game_id]['player2']['message_id'] = waiting_message_id
                except Exception as e:
                    logger.error(f"编辑群组消息失败: {str(e)}")
                    await process_user.async_update_user_coins(user_id, 1)
                    await process_user.async_update_user_coins(waiting_player_id, 1)
                    del active_games[game_id]
                    del game_locks[game_id]
                    await update.message.reply_text("创建游戏失败，请稍后重试。")
                    return

                # 私聊发送选择按钮
                try:
                    p1_msg = await context.bot.send_message(
                        chat_id=waiting_player_id,
                        text=f"您正在与 @{username} 对战石头剪刀布。\n请选择您的出招：\n\n游戏规则: 每位玩家消耗1金币，获胜者获得2金币奖励，平局各退还1金币。\n⚠️ 请在2分钟内做出选择，否则游戏将取消并退还金币。",
                        reply_markup=get_choice_keyboard(waiting_player_id)
                    )
                    p2_msg = await context.bot.send_message(
                        chat_id=user_id,
                        text=f"您正在与 @{waiting_player_name} 对战石头剪刀布。\n请选择您的出招：\n\n游戏规则: 每位玩家消耗1金币，获胜者获得2金币奖励，平局各退还1金币。\n⚠️ 请在2分钟内做出选择，否则游戏将取消并退还金币。",
                        reply_markup=get_choice_keyboard(user_id)
                    )
                    active_games[game_id]['player1']['private_msg_id'] = p1_msg.message_id
                    active_games[game_id]['player2']['private_msg_id'] = p2_msg.message_id
                except Exception as e:
                    logger.error(f"发送私聊消息失败: {str(e)}")
            else:
                try:
                    await context.bot.edit_message_text(
                        chat_id=waiting_chat_id,
                        message_id=waiting_message_id,
                        text=f"游戏开始！您正在与 @{username} 对战。\n请选择您的出招：\n\n游戏规则: 每位玩家消耗1金币，获胜者获得2金币奖励，平局各退还1金币。\n⚠️ 请在2分钟内做出选择，否则游戏将取消并退还金币。",
                        reply_markup=get_choice_keyboard(waiting_player_id)
                    )
                    p2_msg = await update.message.reply_text(
                        text=f"游戏开始！您正在与 @{waiting_player_name} 对战。\n请选择您的出招：\n\n游戏规则: 每位玩家消耗1金币，获胜者获得2金币奖励，平局各退还1金币。\n⚠️ 请在2分钟内做出选择，否则游戏将取消并退还金币。",
                        reply_markup=get_choice_keyboard(user_id)
                    )
                    active_games[game_id]['player2']['message_id'] = p2_msg.message_id
                except Exception as e:
                    logger.error(f"消息处理失败: {str(e)}")
                    await process_user.async_update_user_coins(user_id, 1)
                    await process_user.async_update_user_coins(waiting_player_id, 1)
                    del active_games[game_id]
                    del game_locks[game_id]
                    await update.message.reply_text("创建游戏失败，请稍后重试。")
                    return

            waiting_room = None
            game_timeouts[game_id] = context.application.create_task(game_timeout(context, game_id, 120))
            return

        # 创建等待房间
        waiting_msg = await update.message.reply_text(
            text="🎲 等待其他玩家加入石头剪刀布游戏...\n输入 /rps_game 或点击下方按钮加入\n\n游戏规则: 每位玩家消耗1金币，获胜者获得2金币奖励，平局各退还1金币。",
            reply_markup=get_waiting_keyboard()
        )
        waiting_room = {'player_id': user_id, 'player_name': username, 'chat_id': chat_id, 'message_id': waiting_msg.message_id}
        context.application.create_task(cancel_waiting_game(context, user_id, waiting_msg.message_id))

async def rps_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理回调查询"""
    query = update.callback_query
    callback_data = query.data
    user_id = query.from_user.id

    if callback_data == "rps_join":
        asyncio.create_task(handle_join_callback(update, context))
    elif callback_data == "rps_cancel":
        asyncio.create_task(handle_cancel_callback(update, context))
    elif callback_data.startswith("rps_choice_"):
        parts = callback_data.split("_")
        if len(parts) >= 4:
            choice, button_user_id = parts[2], int(parts[3])
            if user_id != button_user_id:
                await query.answer("这不是您的按钮", show_alert=True)
                return
            asyncio.create_task(handle_choice_callback(update, context, choice))
        else:
            await query.answer("无效的回调数据", show_alert=True)

async def handle_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理加入游戏"""
    global waiting_room
    query = update.callback_query
    user_id = query.from_user.id
    username = query.from_user.username or query.from_user.first_name

    if not await process_user.async_user_exists(user_id):
        await query.answer("请先使用 /me 命令注册后再游玩", show_alert=True)
        return
    user_coins = await process_user.async_get_user_coins(user_id)
    if user_coins < 1:
        await query.answer("您的金币不足，需要至少1枚金币才能开始游戏", show_alert=True)
        return
    if any(user_id in [game['player1']['id'], game.get('player2', {}).get('id')] for game in active_games.values()):
        await query.answer("您已经在一个游戏中，请先完成该游戏", show_alert=True)
        return

    async with waiting_room_lock:
        if not waiting_room:
            await query.answer("该游戏已开始或已被取消", show_alert=True)
            return
        if waiting_room['player_id'] == user_id:
            await query.answer("这是您自己创建的游戏，请等待他人加入", show_alert=True)
            return

        game_id = random.randint(10000, 99999)
        waiting_player_id = waiting_room['player_id']
        waiting_player_name = waiting_room['player_name']
        waiting_chat_id = waiting_room['chat_id']
        waiting_message_id = waiting_room['message_id']
        same_chat = (waiting_chat_id == query.message.chat.id)

        await process_user.async_update_user_coins(user_id, -1)
        await process_user.async_update_user_coins(waiting_player_id, -1)

        game_locks[game_id] = asyncio.Lock()
        active_games[game_id] = {
            'state': CHOOSING,
            'player1': {'id': waiting_player_id, 'name': waiting_player_name, 'chat_id': waiting_chat_id, 'message_id': waiting_message_id, 'choice': None},
            'player2': {'id': user_id, 'name': username, 'chat_id': query.message.chat.id, 'message_id': query.message.message_id, 'choice': None},
            'same_chat': same_chat
        }

        if same_chat:
            try:
                await query.edit_message_text(
                    text=f"🎮 石头剪刀布游戏开始！\n\n玩家1: @{waiting_player_name} (未选择)\n玩家2: @{username} (未选择)\n\n游戏规则: 每位玩家消耗1金币，获胜者获得2金币奖励。\n请双方查看私聊消息进行选择。",
                    reply_markup=None
                )
                p1_msg = await context.bot.send_message(
                    chat_id=waiting_player_id,
                    text=f"您正在与 @{username} 对战石头剪刀布。\n请选择您的出招：\n\n游戏规则: 每位玩家消耗1金币，获胜者获得2金币奖励，平局各退还1金币。\n⚠️ 请在2分钟内做出选择，否则游戏将取消并退还金币。",
                    reply_markup=get_choice_keyboard(waiting_player_id)
                )
                p2_msg = await context.bot.send_message(
                    chat_id=user_id,
                    text=f"您正在与 @{waiting_player_name} 对战石头剪刀布。\n请选择您的出招：\n\n游戏规则: 每位玩家消耗1金币，获胜者获得2金币奖励，平局各退还1金币。\n⚠️ 请在2分钟内做出选择，否则游戏将取消并退还金币。",
                    reply_markup=get_choice_keyboard(user_id)
                )
                active_games[game_id]['player1']['private_msg_id'] = p1_msg.message_id
                active_games[game_id]['player2']['private_msg_id'] = p2_msg.message_id
            except Exception as e:
                logger.error(f"创建游戏失败: {str(e)}")
                await process_user.async_update_user_coins(user_id, 1)
                await process_user.async_update_user_coins(waiting_player_id, 1)
                del active_games[game_id]
                del game_locks[game_id]
                await query.answer("创建游戏失败，请稍后重试", show_alert=True)
                return
        else:
            try:
                await context.bot.edit_message_text(
                    chat_id=waiting_chat_id,
                    message_id=waiting_message_id,
                    text=f"游戏开始！您正在与 @{username} 对战。\n请选择您的出招：\n\n游戏规则: 每位玩家消耗1金币，获胜者获得2金币奖励，平局各退还1金币。\n⚠️ 请在2分钟内做出选择，否则游戏将取消并退还金币。",
                    reply_markup=get_choice_keyboard(waiting_player_id)
                )
                await query.edit_message_text(
                    text=f"游戏开始！您正在与 @{waiting_player_name} 对战。\n请选择您的出招：\n\n游戏规则: 每位玩家消耗1金币，获胜者获得2金币奖励，平局各退还1金币。\n⚠️ 请在2分钟内做出选择，否则游戏将取消并退还金币。",
                    reply_markup=get_choice_keyboard(user_id)
                )
            except Exception as e:
                logger.error(f"消息处理失败: {str(e)}")
                await process_user.async_update_user_coins(user_id, 1)
                await process_user.async_update_user_coins(waiting_player_id, 1)
                del active_games[game_id]
                del game_locks[game_id]
                await query.answer("创建游戏失败，请稍后重试", show_alert=True)
                return

        waiting_room = None
        game_timeouts[game_id] = context.application.create_task(game_timeout(context, game_id, 120))

async def handle_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理取消等待"""
    global waiting_room
    query = update.callback_query
    user_id = query.from_user.id

    async with waiting_room_lock:
        if not waiting_room or waiting_room['player_id'] != user_id:
            await query.answer("您不是当前等待房间的创建者", show_alert=True)
            return
        await query.edit_message_text(text="石头剪刀布游戏等待已取消。")
        waiting_room = None

async def handle_choice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, choice: str):
    """处理玩家选择"""
    query = update.callback_query
    user_id = query.from_user.id

    game_id = next((gid for gid, game in active_games.items() if user_id in [game['player1']['id'], game.get('player2', {}).get('id')]), None)
    if not game_id:
        await query.answer("您不在任何活跃的游戏中", show_alert=True)
        return

    async with game_locks[game_id]:
        game = active_games[game_id]
        player_role = 'player1' if game['player1']['id'] == user_id else 'player2'
        opponent_role = 'player2' if player_role == 'player1' else 'player1'

        if game['state'] != CHOOSING:
            await query.answer("游戏已经结束", show_alert=True)
            return
        if game[player_role]['choice']:
            await query.answer("您已经做出了选择", show_alert=True)
            return

        game[player_role]['choice'] = choice
        await query.answer(f"您选择了 {EMOJI_MAP[choice]}", show_alert=True)

        if game.get('same_chat', False):
            p1_status = "✓ 已选择" if game['player1']['choice'] else "(未选择)"
            p2_status = "✓ 已选择" if game['player2']['choice'] else "(未选择)"
            try:
                await context.bot.edit_message_text(
                    chat_id=game['player1']['chat_id'],
                    message_id=game['player1']['message_id'],
                    text=f"🎮 石头剪刀布游戏进行中！\n\n玩家1: @{game['player1']['name']} {p1_status}\n玩家2: @{game['player2']['name']} {p2_status}\n\n游戏规则: 每位玩家消耗1金币，获胜者获得2金币奖励，平局各退还1金币。",
                    reply_markup=None
                )
                await context.bot.edit_message_text(
                    chat_id=user_id,
                    message_id=game[player_role]['private_msg_id'],
                    text=f"您正在与 @{game[opponent_role]['name']} 对战。\n已选择：{EMOJI_MAP[choice]}\n等待对方做出选择...",
                    reply_markup=None
                )
            except Exception as e:
                logger.error(f"更新消息失败: {str(e)}")
        else:
            try:
                await query.edit_message_text(
                    text=f"您正在与 @{game[opponent_role]['name']} 对战。\n已选择：{EMOJI_MAP[choice]}\n等待对方做出选择...",
                    reply_markup=None
                )
            except Exception as e:
                logger.error(f"更新消息失败: {str(e)}")

        if game['player1']['choice'] and game['player2']['choice']:
            if game_id in game_timeouts and not game_timeouts[game_id].done():
                game_timeouts[game_id].cancel()
                del game_timeouts[game_id]
            await determine_winner(context, game_id)

async def determine_winner(context: ContextTypes.DEFAULT_TYPE, game_id: int):
    """计算游戏结果"""
    game = active_games[game_id]
    p1_choice, p2_choice = game['player1']['choice'], game['player2']['choice']
    result = RESULT_MAP[(p1_choice, p2_choice)]

    if result == "平局":
        await process_user.async_update_user_coins(game['player1']['id'], 1)
        await process_user.async_update_user_coins(game['player2']['id'], 1)
        winner_text = "游戏平局！双方各退还1金币。"
    elif result in ["石头胜", "布胜", "剪刀胜"] and ((p1_choice == ROCK and p2_choice == SCISSORS) or (p1_choice == PAPER and p2_choice == ROCK) or (p1_choice == SCISSORS and p2_choice == PAPER)):
        await process_user.async_update_user_coins(game['player1']['id'], 2)
        winner_text = f"@{game['player1']['name']} 获胜！\n获得2金币奖励。"
    else:
        await process_user.async_update_user_coins(game['player2']['id'], 2)
        winner_text = f"@{game['player2']['name']} 获胜！\n获得2金币奖励."

    result_text = f"🎮 石头剪刀布游戏结果：\n\n@{game['player1']['name']}: {EMOJI_MAP[p1_choice]} vs {EMOJI_MAP[p2_choice]} :@{game['player2']['name']}\n\n{winner_text}"
    game['state'] = GAME_OVER

    if game.get('same_chat', False):
        try:
            await context.bot.edit_message_text(chat_id=game['player1']['chat_id'], message_id=game['player1']['message_id'], text=result_text, reply_markup=None)
            for player_key in ['player1', 'player2']:
                await context.bot.edit_message_text(chat_id=game[player_key]['id'], message_id=game[player_key]['private_msg_id'], text=result_text, reply_markup=None)
        except Exception as e:
            logger.error(f"更新结果失败: {str(e)}")
    else:
        for player_key in ['player1', 'player2']:
            try:
                await context.bot.edit_message_text(chat_id=game[player_key]['chat_id'], message_id=game[player_key]['message_id'], text=result_text, reply_markup=None)
            except Exception as e:
                logger.error(f"更新结果失败: {str(e)}")

    asyncio.create_task(clean_game(game_id))

async def clean_game(game_id: int):
    """清理游戏资源"""
    await asyncio.sleep(5)
    try:
        if game_id in game_timeouts and not game_timeouts[game_id].done():
            game_timeouts[game_id].cancel()
        if game_id in game_timeouts:
            del game_timeouts[game_id]
        if game_id in active_games:
            del active_games[game_id]
        if game_id in game_locks:
            del game_locks[game_id]
    except Exception as e:
        logger.error(f"清理游戏资源出错: {str(e)}")

async def game_timeout(context: ContextTypes.DEFAULT_TYPE, game_id: int, seconds: int):
    """游戏超时处理"""
    await asyncio.sleep(seconds)
    if game_id not in active_games:
        return
    async with game_locks[game_id]:
        game = active_games.get(game_id)
        if not game or game['state'] != CHOOSING or (game['player1']['choice'] and game['player2']['choice']):
            return

        timeout_message = f"🕒 游戏已超时！\n\n玩家1: @{game['player1']['name']} {'已选择' if game['player1']['choice'] else '未选择'}\n玩家2: @{game['player2']['name']} {'已选择' if game['player2']['choice'] else '未选择'}\n\n游戏已取消，已退还双方金币。"
        await process_user.async_update_user_coins(game['player1']['id'], 1)
        await process_user.async_update_user_coins(game['player2']['id'], 1)

        if game.get('same_chat', False):
            try:
                await context.bot.edit_message_text(chat_id=game['player1']['chat_id'], message_id=game['player1']['message_id'], text=timeout_message, reply_markup=None)
                for player_key in ['player1', 'player2']:
                    await context.bot.edit_message_text(chat_id=game[player_key]['id'], message_id=game[player_key]['private_msg_id'], text=f"{timeout_message}\n请重新发起游戏。", reply_markup=None)
            except Exception as e:
                logger.error(f"超时消息更新失败: {str(e)}")
        else:
            for player_key in ['player1', 'player2']:
                try:
                    await context.bot.edit_message_text(chat_id=game[player_key]['chat_id'], message_id=game[player_key]['message_id'], text=timeout_message, reply_markup=None)
                except Exception as e:
                    logger.error(f"超时消息更新失败: {str(e)}")

        game['state'] = GAME_OVER
        asyncio.create_task(clean_game(game_id))

async def cancel_waiting_game(context: ContextTypes.DEFAULT_TYPE, user_id: int, message_id: int):
    """取消等待房间"""
    global waiting_room
    await asyncio.sleep(600)
    async with waiting_room_lock:
        if waiting_room and waiting_room['player_id'] == user_id and waiting_room['message_id'] == message_id:
            try:
                await context.bot.edit_message_text(chat_id=waiting_room['chat_id'], message_id=message_id, text="⌛ 石头剪刀布游戏邀请已超时取消。", reply_markup=None)
            except Exception as exc:
                logger.debug("更新超时邀请消息失败: %s", exc)
            waiting_room = None

def setup_rps_game_handlers(application):
    """注册处理器"""
    application.add_handler(CommandHandler("rps_game", rps_game_command))
    application.add_handler(CallbackQueryHandler(rps_callback_handler, pattern=r"^rps_"))
