import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from telegram import Bot, Update
from telegram.ext import ContextTypes

import biance_api
import config
from telegram_utils import safe_send_markdown, partial_send

logger = logging.getLogger(__name__)

ADMIN_USER_ID = config.ADMIN_USER_ID
CHAT_ID = None
monitor_thread = None
executor = ThreadPoolExecutor(max_workers=1)
lock_until = 0


async def send_message_to_group(message: str):
    bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
    if not CHAT_ID:
        return
    await safe_send_markdown(
        partial_send(bot.send_message, CHAT_ID),
        message,
        logger=logger,
    )


async def delayed_check_result(trigger_time, trigger_price):
    await asyncio.sleep(600)  # 10分钟异步等待
    msg = biance_api.check_result(trigger_time, trigger_price)
    await send_message_to_group(msg)


async def run_monitor_with_notification():
    global monitor_thread, lock_until
    while monitor_thread:
        # 若还在锁定时间内，则跳过检测
        if time.time() < lock_until:
            await asyncio.sleep(5)
            continue

        loop = asyncio.get_event_loop()
        results, trigger_data = await loop.run_in_executor(
            executor, biance_api.monitor_btc_pattern
        )

        # 先输出检测信息
        if results:
            for r in results:
                await send_message_to_group(r)

        # 若检测到触发信息，则10分钟内不再触发
        if trigger_data:
            trigger_price, trigger_time = trigger_data
            asyncio.create_task(delayed_check_result(trigger_time, trigger_price))
            lock_until = time.time() + 600  # 锁定10分钟

        await asyncio.sleep(5)


async def start_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("您没有权限执行此操作")
        return

    global monitor_thread, CHAT_ID
    CHAT_ID = update.effective_chat.id
    if monitor_thread and not monitor_thread.done():
        await update.message.reply_text("BTCUSDT事件合约价格模式监控已在运行")
        return
    monitor_thread = asyncio.create_task(run_monitor_with_notification())
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
