import asyncio
import logging
import time

from telegram import Bot, Update
from telegram.ext import CommandHandler, ContextTypes

from fogmoe_telegram_bot.core import background, blocking, config
from fogmoe_telegram_bot.core.telegram_utils import safe_send_markdown, partial_send
from fogmoe_telegram_bot.features.crypto import biance_api

logger = logging.getLogger(__name__)

ADMIN_USER_ID = config.ADMIN_USER_ID
CHAT_ID = None
# 启动监控的那条命令带来的 bot：监控发消息复用它，不再每条消息新建一个 Bot。
_BOT = None
monitor_thread = None
lock_until = 0


async def send_message_to_group(message: str):
    if not CHAT_ID:
        return
    bot = _BOT if _BOT is not None else Bot(token=config.TELEGRAM_BOT_TOKEN)
    await safe_send_markdown(
        partial_send(bot.send_message, CHAT_ID),
        message,
        logger=logger,
    )


async def delayed_check_result(trigger_time, trigger_price):
    await asyncio.sleep(600)  # 10分钟异步等待
    # binance 的客户端是同步的 requests 实现：放进有界线程适配器，不能阻塞事件循环。
    msg = await blocking.io().run(biance_api.check_result, trigger_time, trigger_price)
    await send_message_to_group(msg)


async def run_monitor_with_notification():
    global monitor_thread, lock_until
    while monitor_thread:
        # 若还在锁定时间内，则跳过检测
        if time.time() < lock_until:
            await asyncio.sleep(5)
            continue

        results, trigger_data = await blocking.io().run(biance_api.monitor_btc_pattern)

        # 先输出检测信息
        if results:
            for r in results:
                await send_message_to_group(r)

        # 若检测到触发信息，则10分钟内不再触发
        if trigger_data:
            trigger_price, trigger_time = trigger_data
            background.spawn(
                delayed_check_result(trigger_time, trigger_price),
                name="btc-monitor-check-result",
            )
            lock_until = time.time() + 600  # 锁定10分钟

        await asyncio.sleep(5)


async def start_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("您没有权限执行此操作")
        return

    global monitor_thread, CHAT_ID, _BOT
    CHAT_ID = update.effective_chat.id
    _BOT = context.bot
    if monitor_thread and not monitor_thread.done():
        await update.message.reply_text("BTCUSDT事件合约价格模式监控已在运行")
        return
    monitor_thread = background.spawn(
        run_monitor_with_notification(),
        name="btc-monitor",
    )
    await update.message.reply_text("BTCUSDT事件合约价格模式监控已启动")


async def stop_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("您没有权限执行此操作")
        return

    global monitor_thread
    if monitor_thread and not monitor_thread.done():
        monitor_thread.cancel()
        monitor_thread = None
        await update.message.reply_text("BTCUSDT事件合约价格模式监控已停止")
    else:
        await update.message.reply_text("BTCUSDT事件合约价格模式监控未运行")


def setup_monitor_handlers(application) -> None:
    """注册管理员用的行情监控开关。"""

    application.add_handler(CommandHandler("start_test_monitor", start_monitor))
    application.add_handler(CommandHandler("stop_test_monitor", stop_monitor))
