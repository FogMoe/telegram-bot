import asyncio
import logging
import weakref

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from fogmoe_telegram_bot.core import process_user
from fogmoe_telegram_bot.core.command_cooldown import cooldown

from . import rps_games
from .panels import edit_panel
from .rps_games import PAPER, ROCK, SCISSORS, Game, Seat

logger = logging.getLogger(__name__)

# 恢复任务的周期：重启后第一次在 RECOVERY_FIRST_SECONDS 秒后运行，之后每隔 RECOVERY_INTERVAL 秒。
# 它同时是丢失的超时定时器的兜底：已过期的对局最迟一个周期之后被退款。
RECOVERY_INTERVAL = 30
RECOVERY_FIRST_SECONDS = 5
WAITING_ROOM_SECONDS = 600

# 表情映射
EMOJI_MAP = {ROCK: "👊", PAPER: "✋", SCISSORS: "✌️"}

# 等待中的玩家（同一时间只有一个等待房间）。房间里没有任何金币：入场费在有人加入、
# 对局创建的那一刻才扣，所以它只放在内存里，重启丢失的只是一张邀请。
waiting_room = None
waiting_room_lock = asyncio.Lock()

RULES_LINE = "游戏规则: 每位玩家消耗1金币，获胜者获得2金币奖励，平局各退还1金币。"
TIME_LIMIT_LINE = "⚠️ 请在2分钟内做出选择，否则游戏将取消并退还金币。"


# 创建选择按钮键盘：绑定对局 id 与玩家 id，旧对局、他人的按钮都不会作用到当前对局
def get_choice_keyboard(game_id, user_id):
    keyboard = [
        [
            InlineKeyboardButton("石头 👊", callback_data=f"rps_choice_{game_id}_{ROCK}_{user_id}"),
            InlineKeyboardButton("剪刀 ✌️", callback_data=f"rps_choice_{game_id}_{SCISSORS}_{user_id}"),
            InlineKeyboardButton("布 ✋", callback_data=f"rps_choice_{game_id}_{PAPER}_{user_id}")
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


def parse_choice_callback(data: str | None) -> tuple[int, str, int] | None:
    """解析 `rps_choice_<game_id>_<choice>_<user_id>`；旧格式或格式不对返回 None。"""
    parts = (data or "").split("_")
    if len(parts) != 5 or parts[:2] != ["rps", "choice"] or parts[3] not in rps_games.CHOICES:
        return None
    try:
        return int(parts[2]), parts[3], int(parts[4])
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 文案
# ---------------------------------------------------------------------------


def started_group_text(game: Game) -> str:
    return (
        f"🎮 石头剪刀布游戏开始！\n\n玩家1: @{game.p1.name} (未选择)\n玩家2: @{game.p2.name} (未选择)\n\n"
        "游戏规则: 每位玩家消耗1金币，获胜者获得2金币奖励。\n请双方查看私聊消息进行选择。"
    )


def private_prompt_text(opponent_name: str) -> str:
    return (
        f"您正在与 @{opponent_name} 对战石头剪刀布。\n请选择您的出招：\n\n"
        f"{RULES_LINE}\n{TIME_LIMIT_LINE}"
    )


def separate_chat_prompt_text(opponent_name: str) -> str:
    return (
        f"游戏开始！您正在与 @{opponent_name} 对战。\n请选择您的出招：\n\n"
        f"{RULES_LINE}\n{TIME_LIMIT_LINE}"
    )


def progress_group_text(game: Game) -> str:
    p1_status = "✓ 已选择" if game.p1.choice else "(未选择)"
    p2_status = "✓ 已选择" if game.p2.choice else "(未选择)"
    return (
        f"🎮 石头剪刀布游戏进行中！\n\n玩家1: @{game.p1.name} {p1_status}\n"
        f"玩家2: @{game.p2.name} {p2_status}\n\n{RULES_LINE}"
    )


def waiting_for_opponent_text(opponent_name: str, choice: str) -> str:
    return f"您正在与 @{opponent_name} 对战。\n已选择：{EMOJI_MAP[choice]}\n等待对方做出选择..."


def final_text(game: Game) -> str:
    """终结对局写到面板上的文字。"""
    if game.outcome == rps_games.OUTCOME_TIMEOUT:
        return (
            f"🕒 游戏已超时！\n\n玩家1: @{game.p1.name} {'已选择' if game.p1.choice else '未选择'}\n"
            f"玩家2: @{game.p2.name} {'已选择' if game.p2.choice else '未选择'}\n\n"
            "游戏已取消，已退还双方金币。"
        )
    if game.outcome == rps_games.OUTCOME_FAILED:
        return "游戏创建失败，已退还双方金币。请稍后重新发起游戏。"

    c1, c2 = game.p1.choice or "", game.p2.choice or ""
    if game.outcome == rps_games.OUTCOME_DRAW:
        winner_text = "游戏平局！双方各退还1金币。"
    elif game.outcome == rps_games.OUTCOME_P1:
        winner_text = f"@{game.p1.name} 获胜！\n获得2金币奖励。"
    else:
        winner_text = f"@{game.p2.name} 获胜！\n获得2金币奖励。"
    return (
        f"🎮 石头剪刀布游戏结果：\n\n"
        f"@{game.p1.name}: {EMOJI_MAP.get(c1, '?')} vs {EMOJI_MAP.get(c2, '?')} :@{game.p2.name}\n\n"
        f"{winner_text}"
    )


def _panel_lock(game_id: int) -> asyncio.Lock:
    """同一局的面板编辑串行：进度刷新与结果公告不会交错，旧的进度不会盖掉最终结果。

    锁只被正在使用它的协程引用，对局结束后自动回收。
    """
    lock = _panel_locks.get(game_id)
    if lock is None:
        lock = asyncio.Lock()
        _panel_locks[game_id] = lock
    return lock


_panel_locks: weakref.WeakValueDictionary[int, asyncio.Lock] = weakref.WeakValueDictionary()


async def announce_game(bot, game_id: int) -> None:
    """把终结的结果写到面板上；全部编辑成功（或被 Telegram 明确拒绝）后记为已公告。"""
    lock = _panel_lock(game_id)
    async with lock:
        await _announce_locked(bot, game_id)


async def _announce_locked(bot, game_id: int) -> None:
    game = await rps_games.load_game(game_id)
    if game is None or game.is_choosing or game.announced:
        return
    text = final_text(game)
    suffix = "\n请重新发起游戏。" if game.outcome == rps_games.OUTCOME_TIMEOUT else ""
    if game.same_chat:
        targets = [
            (game.p1.chat_id, game.p1.message_id, ""),
            (game.p1.user_id, game.p1.private_msg_id, suffix),
            (game.p2.user_id, game.p2.private_msg_id, suffix),
        ]
    else:
        targets = [
            (game.p1.chat_id, game.p1.message_id, ""),
            (game.p2.chat_id, game.p2.message_id, ""),
        ]
    done = True
    for chat_id, message_id, extra in targets:
        edited = await edit_panel(
            bot, chat_id=chat_id, message_id=message_id, text=text + extra, reply_markup=None
        )
        done = done and edited
    if done:
        await rps_games.mark_announced(game_id)


# ---------------------------------------------------------------------------
# 超时与恢复
# ---------------------------------------------------------------------------


async def game_timeout_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """超时定时器：到期还没分出胜负就退还双方入场费。"""
    game_id = context.job.data
    await rps_games.expire_game(game_id)
    await announce_game(context.bot, game_id)


async def recover_rps_games(context: ContextTypes.DEFAULT_TYPE) -> None:
    """启动后与周期性的恢复：退款已过期的对局，补发还没写到面板上的结果。"""
    await rps_games.expire_due_games()
    for game_id in await rps_games.unannounced_game_ids():
        try:
            await announce_game(context.bot, game_id)
        except Exception:
            logger.exception("补发对局 %s 的结果失败", game_id)


# ---------------------------------------------------------------------------
# 开局
# ---------------------------------------------------------------------------


async def _drop_waiting_room(bot, text: str) -> None:
    """清掉等待房间并把邀请消息改成 text（调用方持有 waiting_room_lock）。"""
    global waiting_room
    room, waiting_room = waiting_room, None
    if room:
        await edit_panel(
            bot, chat_id=room['chat_id'], message_id=room['message_id'], text=text, reply_markup=None
        )


async def _explain_rejection(context, rejection, *, joiner_id: int, notify) -> None:
    code, who = rejection.code, rejection.user_id
    if who == joiner_id:
        if code == rps_games.CODE_BUSY:
            await notify("您已经在一个游戏中，请先完成该游戏。")
        elif code == rps_games.CODE_NO_USER:
            await notify("请先使用 /me 命令注册后再游玩。")
        else:
            await notify("您的金币不足，需要至少1枚金币才能开始游戏。")
        return

    # 发起者这边不满足开局条件：邀请已经过时，撤掉它。
    if code == rps_games.CODE_BUSY:
        reason = "发起者已在另一局游戏中"
    elif code == rps_games.CODE_NO_USER:
        reason = "发起者尚未注册"
    else:
        reason = "发起者金币不足"
    await _drop_waiting_room(context.bot, f"⌛ 石头剪刀布游戏邀请已取消：{reason}。")
    await notify("对方暂时无法开始游戏，邀请已取消。")


async def _launch_ui(context, game: Game, send_to_joiner) -> bool:
    """给两名玩家发出选择面板，并记录消息 id。任何一步失败返回 False。"""
    bot = context.bot
    p1, p2 = game.p1, game.p2
    try:
        if game.same_chat:
            await bot.edit_message_text(
                chat_id=p1.chat_id,
                message_id=p1.message_id,
                text=started_group_text(game),
                reply_markup=None,
            )
            # 私聊发送选择按钮
            p1_msg = await bot.send_message(
                chat_id=p1.user_id,
                text=private_prompt_text(p2.name),
                reply_markup=get_choice_keyboard(game.id, p1.user_id),
            )
            await rps_games.record_message_ids(game.id, p1_private_msg_id=p1_msg.message_id)
            p2_msg = await bot.send_message(
                chat_id=p2.user_id,
                text=private_prompt_text(p1.name),
                reply_markup=get_choice_keyboard(game.id, p2.user_id),
            )
            await rps_games.record_message_ids(game.id, p2_private_msg_id=p2_msg.message_id)
        else:
            await bot.edit_message_text(
                chat_id=p1.chat_id,
                message_id=p1.message_id,
                text=separate_chat_prompt_text(p2.name),
                reply_markup=get_choice_keyboard(game.id, p1.user_id),
            )
            p2_msg = await send_to_joiner(
                text=separate_chat_prompt_text(p1.name),
                reply_markup=get_choice_keyboard(game.id, p2.user_id),
            )
            await rps_games.record_message_ids(game.id, p2_message_id=p2_msg.message_id)
    except Exception as exc:
        logger.error("发送石头剪刀布面板失败 game=%s: %s", game.id, exc)
        return False
    return True


async def _start_game(
    context,
    *,
    joiner_id: int,
    joiner_name: str,
    joiner_chat_id: int,
    send_to_joiner,
    notify,
) -> None:
    """有人加入等待房间：创建对局（含两名玩家的入场扣款）并发出面板。

    调用方持有 waiting_room_lock，且 waiting_room 不为空。
    """
    global waiting_room
    room = waiting_room
    same_chat = room['chat_id'] == joiner_chat_id
    p1 = Seat(room['player_id'], room['player_name'], room['chat_id'], room['message_id'])
    p2 = Seat(
        joiner_id,
        joiner_name,
        joiner_chat_id,
        message_id=room['message_id'] if same_chat else None,
    )
    try:
        game = await rps_games.create_game(p1, p2, same_chat=same_chat)
    except rps_games.StartRejected as rejection:
        await _explain_rejection(context, rejection, joiner_id=joiner_id, notify=notify)
        return
    except Exception:
        logger.exception("创建石头剪刀布对局失败")
        await notify("创建游戏失败，请稍后重试。")
        return

    waiting_room = None
    if not await _launch_ui(context, game, send_to_joiner):
        # 入场费已经扣了：退还并把对局标记为失败，面板上写明结果。
        await rps_games.cancel_game(game.id)
        await announce_game(context.bot, game.id)
        await notify("创建游戏失败，请稍后重试。")
        return

    context.job_queue.run_once(
        game_timeout_job,
        when=game.seconds_left + 0.5,
        data=game.id,
        name=f"rps_game_{game.id}",
    )


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
    if await rps_games.active_game_for(user_id):
        await update.message.reply_text("您已经在一个游戏中，请先完成该游戏。")
        return

    async with waiting_room_lock:
        if waiting_room and waiting_room['player_id'] == user_id:
            await update.message.reply_text("您已经创建了一个游戏等待中，请等待其他玩家加入或取消当前游戏。")
            return

        # 如果有等待玩家，匹配并开始游戏
        if waiting_room:
            await _start_game(
                context,
                joiner_id=user_id,
                joiner_name=username,
                joiner_chat_id=chat_id,
                send_to_joiner=update.message.reply_text,
                notify=update.message.reply_text,
            )
            return

        # 创建等待房间
        waiting_msg = await update.message.reply_text(
            text=f"🎲 等待其他玩家加入石头剪刀布游戏...\n输入 /rps_game 或点击下方按钮加入\n\n{RULES_LINE}",
            reply_markup=get_waiting_keyboard()
        )
        waiting_room = {'player_id': user_id, 'player_name': username, 'chat_id': chat_id, 'message_id': waiting_msg.message_id}
        context.job_queue.run_once(
            cancel_waiting_job,
            when=WAITING_ROOM_SECONDS,
            data=(user_id, waiting_msg.message_id),
            name=f"rps_waiting_{user_id}_{waiting_msg.message_id}",
        )


async def rps_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理回调查询"""
    query = update.callback_query
    callback_data = query.data

    if callback_data == "rps_join":
        await handle_join_callback(update, context)
    elif callback_data == "rps_cancel":
        await handle_cancel_callback(update, context)
    elif callback_data.startswith("rps_choice_"):
        parsed = parse_choice_callback(callback_data)
        if parsed is None:
            await query.answer("按钮已失效，请重新发起游戏。", show_alert=True)
            return
        game_id, choice, button_user_id = parsed
        if query.from_user.id != button_user_id:
            await query.answer("这不是您的按钮", show_alert=True)
            return
        await handle_choice_callback(update, context, game_id, choice)


async def handle_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理加入游戏"""
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
    if await rps_games.active_game_for(user_id):
        await query.answer("您已经在一个游戏中，请先完成该游戏", show_alert=True)
        return

    async def notify(text: str) -> None:
        await query.answer(text, show_alert=True)

    async with waiting_room_lock:
        # 只有等待房间自己的那条消息上的按钮才有效；其他（已取消、已过期）的邀请消息不能加入当前房间。
        if (
            not waiting_room
            or waiting_room['chat_id'] != query.message.chat.id
            or waiting_room['message_id'] != query.message.message_id
        ):
            await query.answer("该游戏已开始或已被取消", show_alert=True)
            return
        if waiting_room['player_id'] == user_id:
            await query.answer("这是您自己创建的游戏，请等待他人加入", show_alert=True)
            return

        await _start_game(
            context,
            joiner_id=user_id,
            joiner_name=username,
            joiner_chat_id=query.message.chat.id,
            send_to_joiner=lambda **kwargs: context.bot.send_message(
                chat_id=query.message.chat.id, **kwargs
            ),
            notify=notify,
        )


async def handle_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理取消等待"""
    global waiting_room
    query = update.callback_query
    user_id = query.from_user.id

    async with waiting_room_lock:
        if (
            not waiting_room
            or waiting_room['player_id'] != user_id
            or waiting_room['message_id'] != query.message.message_id
        ):
            await query.answer("您不是当前等待房间的创建者", show_alert=True)
            return
        waiting_room = None
    await query.edit_message_text(text="石头剪刀布游戏等待已取消。")


async def handle_choice_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE, game_id: int, choice: str
):
    """处理玩家选择"""
    query = update.callback_query
    user_id = query.from_user.id

    result = await rps_games.record_choice(game_id, user_id, choice)

    if result.code in (rps_games.CHOICE_NOT_FOUND, rps_games.CHOICE_NOT_PLAYER):
        await query.answer("您不在任何活跃的游戏中", show_alert=True)
        return
    if result.code == rps_games.CHOICE_FINISHED:
        await query.answer("游戏已经结束", show_alert=True)
        await announce_game(context.bot, game_id)
        return
    if result.code == rps_games.CHOICE_ALREADY:
        await query.answer("您已经做出了选择", show_alert=True)
        return
    if result.code == rps_games.CHOICE_EXPIRED:
        await query.answer("游戏已超时，已退还双方金币", show_alert=True)
        await announce_game(context.bot, game_id)
        return

    await query.answer(f"您选择了 {EMOJI_MAP[choice]}", show_alert=True)
    if result.code == rps_games.CHOICE_SETTLED:
        await announce_game(context.bot, game_id)
        return

    # 只有一方做出了选择：更新面板上的选择状态。被点击的消息就是这名玩家自己的选择面板。
    # 上面等待 answer 的时候对方可能已经出招并公告了结果：在锁里读最新状态，已经结束就只补公告。
    lock = _panel_lock(game_id)
    async with lock:
        latest = await rps_games.load_game(game_id)
        if latest is None:
            return
        if not latest.is_choosing:
            await _announce_locked(context.bot, game_id)
            return
        opponent = latest.opponent_of(user_id)
        if latest.same_chat:
            await edit_panel(
                context.bot,
                chat_id=latest.p1.chat_id,
                message_id=latest.p1.message_id,
                text=progress_group_text(latest),
                reply_markup=None,
            )
        await edit_panel(
            context.bot,
            chat_id=query.message.chat.id,
            message_id=query.message.message_id,
            text=waiting_for_opponent_text(opponent.name, choice),
            reply_markup=None,
        )


async def cancel_waiting_job(context: ContextTypes.DEFAULT_TYPE):
    """取消等待房间：邀请超过 10 分钟没人加入。"""
    global waiting_room
    user_id, message_id = context.job.data
    async with waiting_room_lock:
        if waiting_room and waiting_room['player_id'] == user_id and waiting_room['message_id'] == message_id:
            await _drop_waiting_room(context.bot, "⌛ 石头剪刀布游戏邀请已超时取消。")


def setup_rps_game_handlers(application):
    """注册处理器与恢复任务"""
    application.add_handler(CommandHandler("rps_game", rps_game_command))
    application.add_handler(CallbackQueryHandler(rps_callback_handler, pattern=r"^rps_"))
    application.job_queue.run_repeating(
        recover_rps_games, interval=RECOVERY_INTERVAL, first=RECOVERY_FIRST_SECONDS
    )
