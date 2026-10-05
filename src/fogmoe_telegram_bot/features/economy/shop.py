"""/shop 的 Telegram 适配层：解析按钮回调、调用购买操作、回复与编辑消息。

购买规则与事务在 `operations/shop.py`，菜单与文案在 `shop_views.py`；这里只做输入映射和投递。
"""

import asyncio
import logging
import time

from telegram import Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from fogmoe_telegram_bot.core.command_cooldown import cooldown
from fogmoe_telegram_bot.core.redaction import log_exception, report_error

from . import shop_views
from .operations import shop as shop_purchases
from .shop_views import Action

logger = logging.getLogger(__name__)
# 进程内的购买锁：保护保底计数（进程内存状态）。余额与业务状态的一致性由数据库事务保证。
lock = asyncio.Lock()

# 用户最后抽奖消息记录
# 格式: {(user_id, chat_id): {'message_id': 消息ID, 'timestamp': 最后发送时间, 'message_type': '消息类型', 'text': 文本}}
last_lottery_messages: dict[tuple[int, int], dict] = {}

# 设置消息更新阈值（秒）- 超过这个时间才会发送新消息
MESSAGE_UPDATE_THRESHOLD = 30


@cooldown
async def shop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /shop 命令：发送商城一级菜单
    """
    menu = shop_views.home_menu()
    await update.message.reply_text(menu.text, reply_markup=menu.keyboard)


async def _post_lottery_record(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    user_id: int,
    line: str,
) -> None:
    """把这次购彩记进聊天里的「最近的彩票记录」。

    30 秒内合并编辑同一条消息；已满 6 行或编辑失败时新发一条。
    """
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


async def _show_menu(query, menu: shop_views.Menu) -> None:
    try:
        await query.edit_message_text(menu.text, reply_markup=menu.keyboard)
    except Exception as exc:
        logger.debug("刷新商店菜单失败: %s", exc)


async def _close(query) -> None:
    try:
        await query.delete_message()
    except Exception as exc:
        logger.debug("关闭商城消息失败: %s", exc)


async def _buy_memory_limit(query, user_id: int) -> None:
    request = shop_purchases.MemoryLimitPurchase(
        user_id, shop_purchases.shop_op_key("memory", query.id)
    )
    async with lock:
        try:
            result = await shop_purchases.buy_memory_limit(request)
        except Exception:
            log_exception(logger, f"购买记忆上限失败: user_id={user_id}")
            await query.answer("购买出现错误，请稍后再试。", show_alert=True)
        else:
            await query.answer(shop_views.memory_limit_message(result), show_alert=True)


async def _upgrade_permission(query, user_id: int, level: int) -> None:
    request = shop_purchases.PermissionUpgrade(
        user_id, level, shop_purchases.shop_op_key(f"perm{level}", query.id)
    )
    async with lock:
        try:
            result = await shop_purchases.upgrade_permission(request)
        except Exception:
            log_exception(logger, f"购买权限升级失败: user_id={user_id} level={level}")
            await query.answer("购买出现错误，请稍后再试。", show_alert=True)
        else:
            await query.answer(shop_views.permission_message(result), show_alert=True)


async def _announce_ticket(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    user_id: int,
    result: shop_purchases.TicketResult,
    view: shop_views.TicketView,
) -> bool:
    """弹出购买结果；购买被拒绝时返回 False，成功时还会把这次购彩记进聊天并返回 True。"""
    await query.answer(shop_views.ticket_message(result, view), show_alert=True)
    if result.status is not shop_purchases.PurchaseStatus.PURCHASED:
        return False
    user_label = (
        f"@{query.from_user.username}" if query.from_user.username else query.from_user.first_name
    )
    await _post_lottery_record(
        context,
        chat_id=chat_id,
        user_id=user_id,
        line=shop_views.lottery_record_line(user_label, view, result),
    )
    return True


async def _buy_scratch_ticket(query, context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> None:
    """购买刮刮乐：扣除10金币，随机获得0～20金币。"""
    request = shop_purchases.TicketPurchase(
        user_id, shop_purchases.shop_op_key("scratch", query.id)
    )
    async with lock:
        try:
            result = await shop_purchases.buy_scratch_ticket(request)
            await _announce_ticket(
                query,
                context,
                chat_id=chat_id,
                user_id=user_id,
                result=result,
                view=shop_views.SCRATCH_VIEW,
            )
        except Exception as e:
            notice = report_error(logger, "购买刮刮乐时出错", e)
            await query.answer(f"购买刮刮乐时出错，请稍后再试。\n{notice}", show_alert=True)


async def _buy_huanle_ticket(query, context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> None:
    """购买欢乐彩：扣除1金币，根据概率获得奖励。"""
    request = shop_purchases.TicketPurchase(
        user_id, shop_purchases.shop_op_key("huanle", query.id)
    )
    async with lock:
        try:
            result = await shop_purchases.buy_huanle_ticket(request)
            await _announce_ticket(
                query,
                context,
                chat_id=chat_id,
                user_id=user_id,
                result=result,
                view=shop_views.HUANLE_VIEW,
            )
        except Exception:
            logger.exception("购买欢乐彩失败: user_id=%s", user_id)
            await query.answer("购买欢乐彩时出错，请稍后再试。", show_alert=True)


async def shop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    处理商城按钮回调：
    - 菜单按钮：编辑当前消息，显示权限、彩票二级菜单或回到一级菜单。
    - 购买按钮：记忆上限、权限升级、刮刮乐、欢乐彩，结果以弹窗显示；彩票另外记入聊天。
    - “关闭商店”按钮：删除商城消息。
    """
    query = update.callback_query
    parsed = shop_views.parse_callback(query.data)
    if parsed is None:
        return
    user_id = query.from_user.id

    if parsed.action is Action.PERMISSION_MENU:
        await _show_menu(query, shop_views.permission_menu())
    elif parsed.action is Action.LOTTERY_MENU:
        await _show_menu(query, shop_views.lottery_menu())
    elif parsed.action is Action.HOME:
        await _show_menu(query, shop_views.home_menu())
    elif parsed.action is Action.CLOSE:
        await _close(query)
    elif parsed.action is Action.BUY_MEMORY_LIMIT:
        await _buy_memory_limit(query, user_id)
    elif parsed.action is Action.UPGRADE_PERMISSION:
        assert parsed.level is not None
        await _upgrade_permission(query, user_id, parsed.level)
    elif parsed.action is Action.BUY_SCRATCH:
        await _buy_scratch_ticket(query, context, update.effective_chat.id, user_id)
    elif parsed.action is Action.BUY_HUANLE:
        await _buy_huanle_ticket(query, context, update.effective_chat.id, user_id)


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
