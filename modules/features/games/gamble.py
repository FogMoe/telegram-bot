import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from core import process_user
from core.command_cooldown import cooldown

from . import gamble_rounds
from .gamble_rounds import BET_AMOUNTS
from .panels import edit_panel

logger = logging.getLogger(__name__)

# 恢复任务的周期：重启后第一次在 RECOVERY_FIRST_SECONDS 秒后运行，之后每隔 RECOVERY_INTERVAL 秒。
# 它同时是丢失的开奖定时器的兜底：已过截止时间的轮次最迟一个周期之后被结算。
RECOVERY_INTERVAL = 30
RECOVERY_FIRST_SECONDS = 5

# 面板编辑在进程内串行，保证最后一次编辑读到的是最新的下注列表。
_panel_locks: dict[int, asyncio.Lock] = {}

_REJECTION_TEXT = {
    gamble_rounds.CODE_STALE: "该押注面板已失效，请使用 /gamble 开始新一局。",
    gamble_rounds.CODE_CLOSED: "本局已停止接受押注。",
    gamble_rounds.CODE_ALREADY_BET: "您已参与，请等待开奖。",
    gamble_rounds.CODE_INSUFFICIENT: "您的硬币不足",
    gamble_rounds.CODE_NO_USER: "请先使用 /me 命令注册后再参与。",
}


def callback_data(round_id: int, amount: int) -> str:
    return f"gamble_{round_id}_{amount}"


def parse_callback_data(data: str | None) -> tuple[int, int] | None:
    """解析 `gamble_<round_id>_<amount>`；旧格式（只有金额）或格式不对返回 None。"""
    parts = (data or "").split("_")
    if len(parts) != 3 or parts[0] != "gamble":
        return None
    try:
        round_id, amount = int(parts[1]), int(parts[2])
    except ValueError:
        return None
    return round_id, amount


def build_keyboard(round_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(f"押注 {amount} 金币", callback_data=callback_data(round_id, amount))
                for amount in BET_AMOUNTS
            ]
        ]
    )


def _participants_text(bets: tuple[gamble_rounds.Bet, ...]) -> str:
    return "".join(f"@{bet.username} 押注 {bet.amount} 金币\n" for bet in bets)


def open_panel_text(bets: tuple[gamble_rounds.Bet, ...]) -> str:
    return (
        "赌博开始！请点击下面按钮选择押注金额。\n\n当前参与者：\n"
        + (_participants_text(bets) if bets else "暂无")
        + "\n\n开奖时间：5分钟"
    )


def result_text(settlement: gamble_rounds.Settlement) -> str:
    current = settlement.round
    if current.status == gamble_rounds.STATUS_REFUNDED:
        return (
            "本局赌博因故取消，所有押注已退还。\n\n参与详情：\n"
            + _participants_text(settlement.bets)
        )
    winner = settlement.winner
    if winner is None:
        return "本局赌博无人参与！"
    return (
        f"开奖时间到！\n"
        f"中奖者：@{winner.username}\n"
        f"获得奖池所有 {current.prize} 金币！\n\n"
        f"参与详情：\n{_participants_text(settlement.bets)}"
    )


async def refresh_panel(bot, round_id: int) -> None:
    """按数据库里的下注列表刷新开放中的面板。"""
    lock = _panel_locks.setdefault(round_id, asyncio.Lock())
    async with lock:
        current = await gamble_rounds.load_round(round_id)
        if current is None or not current.is_open or current.message_id is None:
            return
        bets = await gamble_rounds.load_bets(round_id)
        await edit_panel(
            bot,
            chat_id=current.chat_id,
            message_id=current.message_id,
            text=open_panel_text(bets),
            reply_markup=build_keyboard(round_id),
        )


async def announce_round(bot, round_id: int) -> None:
    """把终结的结果写到面板上；编辑成功（或被 Telegram 明确拒绝）后记为已公告。"""
    current = await gamble_rounds.load_round(round_id)
    if current is None or current.is_open or current.announced:
        return
    if current.message_id is not None:
        bets = await gamble_rounds.load_bets(round_id)
        settlement = gamble_rounds.Settlement(current, bets, transitioned=False)
        done = await edit_panel(
            bot,
            chat_id=current.chat_id,
            message_id=current.message_id,
            text=result_text(settlement),
        )
        if not done:
            return
    await gamble_rounds.mark_announced(round_id)
    _panel_locks.pop(round_id, None)


async def settle_and_announce(bot, round_id: int, *, only_if_due: bool = False) -> None:
    await gamble_rounds.settle_round(round_id, only_if_due=only_if_due)
    await announce_round(bot, round_id)


async def settle_round_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """开奖定时器：截止时间一到就结算这一轮。"""
    await settle_and_announce(context.bot, context.job.data, only_if_due=True)


async def recover_gamble_rounds(context: ContextTypes.DEFAULT_TYPE) -> None:
    """启动后与周期性的恢复：结算已过截止时间的轮次，补发还没写到面板上的结果。"""
    await gamble_rounds.settle_due_rounds()
    for round_id in await gamble_rounds.unannounced_round_ids():
        try:
            await announce_round(context.bot, round_id)
        except Exception:
            logger.exception("补发轮次 %s 的开奖结果失败", round_id)


@cooldown
async def gamble_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # 判断用户权限是否 >= 1
    if await process_user.get_user_permission(user_id) < 1:
        await update.message.reply_text("您的权限不足，无法使用赌博命令。")
        return

    # 上一局若已过截止时间却还没结算（例如重启之后），先结算掉再判断有没有进行中的局。
    await recover_gamble_rounds(context)

    new_round = await gamble_rounds.open_round(update.effective_chat.id)
    if new_round is None:
        await update.message.reply_text("赌博已在进行中，请等待本局结束。")
        return

    try:
        msg = await update.message.reply_text(
            open_panel_text(()),
            reply_markup=build_keyboard(new_round.id),
        )
    except Exception:
        await gamble_rounds.cancel_round(new_round.id)
        raise
    await gamble_rounds.attach_message(new_round.id, msg.message_id)

    # 到点开奖；进程重启会丢掉这个定时器，由 recover_gamble_rounds 兜底。
    context.job_queue.run_once(
        settle_round_job,
        when=new_round.seconds_left + 0.5,
        data=new_round.id,
        name=f"gamble_round_{new_round.id}",
    )


async def gamble_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    parsed = parse_callback_data(query.data)
    if parsed is None:
        await query.answer(_REJECTION_TEXT[gamble_rounds.CODE_STALE], show_alert=True)
        return
    round_id, amount = parsed

    user = query.from_user
    username = user.username if user.username else user.first_name
    message = query.message
    try:
        await gamble_rounds.accept_bet(
            round_id,
            chat_id=getattr(getattr(message, "chat", None), "id", None),
            message_id=getattr(message, "message_id", None),
            user_id=user.id,
            username=username,
            amount=amount,
        )
    except gamble_rounds.BetRejected as rejection:
        await query.answer(_REJECTION_TEXT[rejection.code], show_alert=True)
        return
    except Exception:
        logger.exception("处理押注失败 round=%s user=%s", round_id, user.id)
        await query.answer("扣除硬币时出错，请稍后再试。", show_alert=True)
        return

    await refresh_panel(context.bot, round_id)
    await query.answer(f"成功押注 {amount} 金币，等待开奖", show_alert=True)


def setup_gamble_handlers(application) -> None:
    """注册赌博玩法的命令、回调与恢复任务。"""

    application.add_handler(CommandHandler("gamble", gamble_command))
    application.add_handler(CallbackQueryHandler(gamble_callback, pattern=r"^gamble_"))
    application.job_queue.run_repeating(
        recover_gamble_rounds, interval=RECOVERY_INTERVAL, first=RECOVERY_FIRST_SECONDS
    )
