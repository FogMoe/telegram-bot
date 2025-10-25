import logging
from logging.handlers import RotatingFileHandler

from telegram import InlineQueryResultArticle, InputTextMessageContent
from uuid import uuid4
from collections import deque
from telegram import Update, Bot, constants
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters, InlineQueryHandler, ChatMemberHandler
import telegram
import mysql_connection
import mysql.connector
import process_user
import ai_chat
import biance_api
import asyncio
from concurrent.futures import ThreadPoolExecutor
import time
import tempfile
import os
import base64
import json
from telegram.ext import CallbackQueryHandler
import gamble
import shop, task
import member_verify
from telegram.ext import ChatMemberHandler
import stake_coin, crypto_predict, config
import swap_fogmoe_solana_token  # 添加导入
import keyword_handler  # 更改导入从keyword到keyword_handler
import spam_control  # 添加导入垃圾信息过滤模块
import omikuji  # 添加导入御神签模块
from command_cooldown import cooldown  # 导入命令冷却装饰器
import rockpaperscissors_game  # 导入石头剪刀布游戏模块
import charge_coin  # 添加导入充值模块
import sicbo  # 导入骰宝游戏模块
import ref  # 导入推广系统模块
import checkin  # 导入每日签到模块
import report  # 导入举报模块
import chart  # 导入代币图表模块
import pic  # 导入图片模块
# import sf  # 导入分享链接检测模块（暂时关闭）
import music  # 导入音乐搜索模块
import rpg  # 导入RPG游戏模块
import developer  # 导入开发者命令模块
import web_password  # 导入Web密码模块
import summary  # 导入会话摘要模块

import group_chat_history
from telegram_utils import safe_send_markdown, partial_send

import bribe  # 新增贿赂模块


logger = logging.getLogger(__name__)


class RateLimiter:
    def __init__(self, max_calls: int, time_window: float):
        self.max_calls = max_calls
        self.time_window = time_window
        self.calls = deque()

    def consume(self) -> bool:
        now = time.time()
        while self.calls and now - self.calls[0] > self.time_window:
            self.calls.popleft()
        if len(self.calls) < self.max_calls:
            self.calls.append(now)
            return True
        return False


_zai_allowance = RateLimiter(max_calls=10, time_window=60.0)

class RateLimiter:
    def __init__(self, max_calls: int, time_window: float):
        self.max_calls = max_calls
        self.time_window = time_window
        self.calls = deque()

    def consume(self) -> bool:
        now = time.time()
        while self.calls and now - self.calls[0] > self.time_window:
            self.calls.popleft()
        if len(self.calls) < self.max_calls:
            self.calls.append(now)
            return True
        return False


# 创建 RotatingFileHandler，最大文件大小设为1MB，最多保留5个备份
handler = RotatingFileHandler(config.BASE_DIR / 'tgbot.log', maxBytes=1*1024*1024, backupCount=5, encoding='utf-8')

log_level = getattr(logging, config.LOG_LEVEL.upper(), logging.INFO)

logging.basicConfig(
    level=log_level,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[handler]
)

ADMIN_USER_ID = config.ADMIN_USER_ID  # 替换为实际管理员的Telegram UserID
# ------------------- Biance监控开始 -------------------
CHAT_ID = None
monitor_thread = None
executor = ThreadPoolExecutor(max_workers=1)


async def send_message_to_group(message: str):
    bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
    if not CHAT_ID:
        return
    await safe_send_markdown(partial_send(bot.send_message, chat_id=CHAT_ID), message, logger=logger)


async def delayed_check_result(trigger_time, trigger_price):
    await asyncio.sleep(600)  # 10分钟异步等待
    msg = biance_api.check_result(trigger_time, trigger_price)
    await send_message_to_group(msg)


lock_until = 0  # 锁定时间，防止频繁触发
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
# ------------------- Biance监控结束 -------------------


async def inline_translate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.inline_query.query
    user_id = update.effective_user.id
    now = time.time()
    
    # 从 context.user_data 获取用户注册状态和上次检查时间
    user_registered = context.user_data.get("is_registered", None)
    last_check_time = context.user_data.get("last_check_time", 0)

    # 如果缓存过期(1小时)或未检查过，则查询数据库
    if user_registered is None or (now - last_check_time > 3600):
        user_registered = await mysql_connection.async_check_user_exists(user_id)
        context.user_data["is_registered"] = user_registered
        context.user_data["last_check_time"] = now

    # 检查用户是否已注册
    if not user_registered:
        results = [
            InlineQueryResultArticle(
                id=str(uuid4()),
                title="请先获取个人信息 Please Register First",
                description="私聊 @FogMoeBot 使用 /me 命令后即可使用翻译功能。 Using the /me command in private chat with @FogMoeBot to translate.",
                input_message_content=InputTextMessageContent(
                    message_text=f"{query}",
                    parse_mode=ParseMode.MARKDOWN
                )
            )
        ]
        await update.inline_query.answer(results, cache_time=300)
        return
    
    # 简单的长度判断，太短就跳过
    if not query or len(query) < 2:
        return
    
    now = time.time()
    last_query_time = context.user_data.get("last_query_time", 0)

    # 若距离上次query不足 2秒，跳过实际翻译，返回提示
    if now - last_query_time < 2:
        results = [
            InlineQueryResultArticle(
                id=str(uuid4()),
                title="请继续输入... Please continue typing...",
                description="停止输入2秒后进行翻译。 Stop typing for 2 seconds before translating.",
                input_message_content=InputTextMessageContent(
                    message_text=f"{query}",
                    parse_mode=ParseMode.MARKDOWN
                )
            )
        ]
        await update.inline_query.answer(results, cache_time=0)
        return

    context.user_data["last_query_time"] = now

    try:
        # 调用异步翻译函数
        translation = await ai_chat.translate_text(query)

        results = [
            InlineQueryResultArticle(
                id=str(uuid4()),
                title="发送翻译结果 Send Translation",
                description=translation[:100] + "..." if len(translation) > 100 else translation,
                input_message_content=InputTextMessageContent(
                    message_text=f"{translation}",
                    parse_mode=ParseMode.MARKDOWN
                )
            )
        ]
        await update.inline_query.answer(results, cache_time=10)

    except Exception as e:
        logging.error(f"内联翻译出错: {str(e)}")
        results = [
            InlineQueryResultArticle(
                id=str(uuid4()),
                title="翻译出错 Translation Error",
                description="翻译服务暂时不可用，请稍后重试 Translation service is temporarily unavailable, please try again later",
                input_message_content=InputTextMessageContent(
                    message_text=f"{query}",
                    parse_mode=ParseMode.MARKDOWN
                )
            )
        ]
        await update.inline_query.answer(results, cache_time=0)



@cooldown
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 检查是否有启动参数（推广邀请码）
    if context.args:
        # 处理推广系统的邀请链接
        await ref.process_start_with_args(update, context)
    
    # 显示欢迎消息
    await context.bot.send_message(chat_id=update.effective_chat.id, text="欢迎使用雾萌机器人喵！！我是雾萌娘，有什么可以帮到您的吗？输入 /help "
                                                                       "我会尽力帮助您的哦。\n"
                                                                       "Welcome to the FogMoeBot! Meow! I'm "
                                                                       "your assistant, is there anything I can "
                                                                       "help you "
                                                                       "with? Type /help and I'll do my best.")


@cooldown
async def admin_announce(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """管理员公告功能，向用户和已知的群组发送"""
    # 验证是否为管理员
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("您没有权限执行此操作\nYou don't have permission to do this.")
        return

    # 检查是否有公告内容
    if not context.args:
        await update.message.reply_text(
            "请在命令后输入要发送的公告内容，例如：\n"
            "/admin_announce 这是一条测试公告\n\n"
            "Please enter the announcement content after the command, for example:\n"
            "/admin_announce This is a test announcement"
        )
        return

    announcement = " ".join(context.args)
    
    # --- 获取目标列表 ---
    user_ids = set()
    group_ids = set()
    
    connection = mysql_connection.create_connection()
    cursor = connection.cursor()
    try:
        # 获取用户ID
        cursor.execute("SELECT id FROM user")
        users = cursor.fetchall()
        user_ids.update(user[0] for user in users)
        
        # 获取群组ID (从多个表中收集)
        group_tables = ['group_keywords', 'group_verification', 'group_spam_control', 'group_chart_tokens']
        for table in group_tables:
            try:
                # 假设这些表都有 group_id 列
                cursor.execute(f"SELECT DISTINCT group_id FROM {table}")
                groups = cursor.fetchall()
                group_ids.update(group[0] for group in groups)
            except mysql.connector.Error as table_err:
                # 如果某个表不存在或查询出错，记录日志并继续
                logging.warning(f"查询群组表 {table} 时出错: {table_err}")
                
    except mysql.connector.Error as db_err:
        logging.error(f"数据库查询出错: {db_err}")
        await update.message.reply_text(f"数据库查询时出错: {db_err}")
        cursor.close()
        connection.close()
        return # 查询出错则不继续发送
    finally:
        cursor.close()
        connection.close()

    # --- 发送公告 --- 
    user_success = 0
    user_fail = 0
    group_success = 0
    group_fail = 0
    
    # 发送给用户
    logging.info(f"开始向 {len(user_ids)} 个用户发送公告...")
    for user_id in user_ids:
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"📢 *公告 Announcement*:\n{announcement}",
                parse_mode=ParseMode.MARKDOWN
            )
            user_success += 1
            await asyncio.sleep(0.1) # 稍微延迟以避免速率限制
        except telegram.error.TelegramError as e:
            logging.warning(f"向用户 {user_id} 发送公告失败: {e}")
            user_fail += 1
        except Exception as e: # 其他可能的错误
             logging.error(f"向用户 {user_id} 发送公告时发生未知错误: {e}")
             user_fail += 1

    # 发送给群组
    logging.info(f"开始向 {len(group_ids)} 个已知群组发送公告...")
    for group_id in group_ids:
        try:
            await context.bot.send_message(
                chat_id=group_id,
                text=f"📢 *群组公告 Group Announcement*:\n{announcement}",
                parse_mode=ParseMode.MARKDOWN
            )
            group_success += 1
            await asyncio.sleep(0.1) # 稍微延迟以避免速率限制
        except telegram.error.TelegramError as e:
            logging.warning(f"向群组 {group_id} 发送公告失败: {e}")
            group_fail += 1
        except Exception as e: # 其他可能的错误
             logging.error(f"向群组 {group_id} 发送公告时发生未知错误: {e}")
             group_fail += 1

    # --- 发送结果报告给管理员 ---
    report_message = (
        f"📢 公告发送完成 Announcement Processed:\n\n"
        f"👤 **用户 Users:**\n"
        f"✅ 成功 Success: {user_success}\n"
        f"❌ 失败 Failed: {user_fail}\n\n"
        f"👥 **群组 Groups:**\n"
        f"✅ 成功 Success: {group_success}\n"
        f"❌ 失败 Failed: {group_fail}"
    )
    await update.message.reply_text(report_message)


@cooldown
async def me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_name = update.effective_user.username
    
    # 检查用户名是否为空
    if not user_name:
        await context.bot.send_message(
            chat_id=update.effective_chat.id, 
            text="您需要设置Telegram用户名才能使用机器人。\n"
                 "请在Telegram设置中设置用户名后再尝试。\n\n"
                 "You need to set a Telegram username to use this bot.\n"
                 "Please set your username in Telegram settings and try again."
        )
        return

    # Connect to the database
    connection = mysql_connection.create_connection()
    cursor = connection.cursor()

    try:
        # Insert user data into the database
        insert_query = """
        INSERT INTO user (id, name) VALUES (%s, %s)
        ON DUPLICATE KEY UPDATE name = VALUES(name)
        """
        cursor.execute(insert_query, (user_id, user_name))
        connection.commit()

        # 查询用户信息
        select_query = """
        SELECT coins, permission FROM user WHERE id = %s
        """
        cursor.execute(select_query, (user_id,))
        result = cursor.fetchone()
        user_coins = result[0] if result else 0
        user_permission = result[1] if result else 0
    except mysql.connector.Error as err:
        if err.errno == 1048:  # Column 'name' cannot be null
            await context.bot.send_message(
                chat_id=update.effective_chat.id, 
                text="您需要设置Telegram用户名才能使用机器人。\n"
                     "请在Telegram设置中设置用户名后再尝试。\n\n"
                     "You need to set a Telegram username to use this bot.\n"
                     "Please set your username in Telegram settings and try again."
            )
            return
        else:
            # 其他数据库错误
            logging.error(f"数据库错误: {err}")
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="发生错误，请稍后再试。\nAn error occurred, please try again later."
            )
            return
    finally:
        cursor.close()
        connection.close()

    await context.bot.send_message(chat_id=update.effective_chat.id, text=f"您的信息如下Your info: \n"
                                                                          f"名字Name: @{user_name}\n"
                                                                          f"金币Coins: {user_coins}\n"
                                                                          f"权限Permission: {user_permission}"
                                   )


@cooldown
async def lottery_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    result = await process_user.async_lottery(user_id)
    await context.bot.send_message(chat_id=update.effective_chat.id, text=result)


@cooldown
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = config.HELP_TEXT
    await safe_send_markdown(
        update.message.reply_text,
        help_text,
        logger=logger,
        fallback_send=partial_send(
            context.bot.send_message,
            chat_id=update.effective_chat.id,
        ),
        disable_web_page_preview=True,
    )


@cooldown
async def github_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send repository link with Markdown formatting."""
    await safe_send_markdown(
        update.message.reply_text,
        "***Open Source***:\n"
        "[AGPL3.0](https://github.com/FogMoe/telegram-bot)",
        logger=logger,
        fallback_send=partial_send(
            context.bot.send_message,
            chat_id=update.effective_chat.id,
        ),
    )


@cooldown
async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    conversation_id = user_id  # Assuming conversation_id is the user_id for simplicity

    # 使用异步方式删除消息记录
    connection = mysql_connection.create_connection()
    cursor = connection.cursor()

    snapshot_created = False

    try:
        cursor.execute(
            "SELECT messages FROM chat_records WHERE conversation_id = %s",
            (conversation_id,),
        )
        snapshot_row = cursor.fetchone()
        conversation_snapshot = None
        if snapshot_row and snapshot_row[0]:
            raw_snapshot = snapshot_row[0]
            if isinstance(raw_snapshot, bytes):
                conversation_snapshot = raw_snapshot.decode("utf-8")
            elif isinstance(raw_snapshot, (dict, list)):
                conversation_snapshot = json.dumps(raw_snapshot, ensure_ascii=False)
            else:
                conversation_snapshot = str(raw_snapshot)

        if conversation_snapshot:
            cursor.execute(
                "INSERT INTO permanent_chat_records (user_id, conversation_snapshot) VALUES (%s, %s)",
                (user_id, conversation_snapshot),
            )
            snapshot_created = True

            cursor.execute(
                """
                DELETE FROM permanent_chat_records
                WHERE user_id = %s
                AND id NOT IN (
                    SELECT recent.id FROM (
                        SELECT id FROM permanent_chat_records
                        WHERE user_id = %s
                        ORDER BY created_at DESC, id DESC
                        LIMIT 100
                    ) AS recent
                )
                """,
                (user_id, user_id),
            )

        # Delete messages for the conversation_id
        delete_query = "DELETE FROM chat_records WHERE conversation_id = %s"
        cursor.execute(delete_query, (conversation_id,))
        connection.commit()
    finally:
        cursor.close()
        connection.close()

    if snapshot_created:
        summary.schedule_summary_generation(user_id)

    await update.message.reply_text("雾萌娘已进行记忆清除处理。\nThe current conversation history has been cleared.")


@cooldown
async def setmyinfo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # 获取用户当前保存的信息
    connection = mysql_connection.create_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT info FROM user WHERE id = %s", (user_id,))
        result = cursor.fetchone()
        current_info = result[0] if result else "无"
        await update.message.reply_text(f"您当前保存的个人自定义信息是Your current personal info is:\n{current_info}")
    finally:
        cursor.close()
        connection.close()

    if not context.args:
        await update.message.reply_text("请在 /setmyinfo 命令后输入要您要保存的个人自定义信息，仅在新对话中有效。\nThe personal information you want to save should be entered after the command, only available in new conversations.\n\n在命令后输入CLEAR可以清空个人自定义信息（例如/setmyinfo CLEAR ）。\nEnter CLEAR after the command to clear the personal information.(e.g./setmyinfo CLEAR)")
        return

    user_info = " ".join(context.args)

    # 如果用户输入CLEAR，则清空info
    if user_info.strip().upper() == "CLEAR":
        user_info = ""

    if len(user_info) > 500:
        await update.message.reply_text("最长500个字符，个人自定义信息长度超过500字符，请重试。\nThe maximum length is 500 characters, the personal information length exceeds 500 characters, please try again.")
        return

    # 更新数据库
    connection = mysql_connection.create_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("UPDATE user SET info = %s WHERE id = %s", (user_info, user_id))
        connection.commit()
        await update.message.reply_text("个人自定义信息已更新。\nPersonal information has been updated.")
    finally:
        cursor.close()
        connection.close()


# 添加一个帮助函数来获取实际的消息对象
def get_effective_message(update: Update):
    """获取有效的消息对象，无论是普通消息还是编辑后的消息"""
    return update.message or update.edited_message


async def should_trigger_ai_response(message_text: str) -> bool:
    """
    使用 Z.ai glm-4.5-flash 模型判断群聊消息是否需要调用主 AI 回复。
    仅返回布尔结果，出现异常时默认不触发回复。
    """
    if not message_text:
        return False

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: _sync_should_trigger_ai_response(message_text)
    )


def _sync_should_trigger_ai_response(message_text: str) -> bool:
    if not _zai_allowance.consume():
        logging.debug("Z.ai rate limiter blocked a request.")
        return False
    client = ai_chat.create_zai_client()
    try:
        response = client.chat.completions.create(
            model="glm-4.5-flash",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是一个简洁的分类器。判断给定消息是否需要雾萌娘机器人主动回复。"
                        "仅在遇到相关问题必要时才回复，例如和AI聊天、寻求帮助、提问或请求信息等。"
                        "如果需要回复，请只回答 YES；如果不需要，请只回答 NO。"
                        "不要输出任何额外解释。"
                    ),
                },
                {
                    "role": "user",
                    "content": message_text,
                },
            ],
            temperature=0.0,
        )
        content = response.choices[0].message.content.strip().lower()
        return content.startswith("yes") or content.startswith("是")
    except Exception as exc:
        logging.error("Z.ai 检测是否应回复失败: %s", exc)
        return False


async def reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # 使用帮助函数获取有效消息
    effective_message = get_effective_message(update)
    if not effective_message:
        logging.warning("收到无效的消息更新，忽略处理")
        return
        
    # 如果聊天是群组，则只对包含触发词时进行回复，
    if update.effective_chat.type in ("group", "supergroup"):
        bot = await context.bot.get_me()
        # 记录群聊上下文
        await group_chat_history.log_group_message(effective_message, update.effective_chat.id)
        # 如果消息是回复给机器人的，则直接处理
        if effective_message.reply_to_message and effective_message.reply_to_message.from_user.id == bot.id:
            pass
        else:
            text = effective_message.text if effective_message.text else ""
            if not text:
                return
            if not ("/fogmoebot" in text or "@FogMoeBot" in text or "雾萌" in text or "机器人" in text or "ai" in text.lower() or "模型" in text or "bot" in text.lower()):
                # should_respond = await should_trigger_ai_response(text)
                # if not should_respond:
                return
            
    # 添加：检查用户是否在聊天冷却期内
    from command_cooldown import check_chat_cooldown
    if not await check_chat_cooldown(update):
        return  # 用户在冷却期内，直接返回

    user_id = update.effective_user.id
    user_name = update.effective_user.username or "EmptyUsername"  # 提供默认值，防止None值导致格式化错误
    # 确保消息时间安全获取
    message_time = effective_message.date.strftime('%Y-%m-%d %H:%M:%S') if effective_message.date else time.strftime('%Y-%m-%d %H:%M:%S')
    conversation_id = user_id

    history_warning_levels_sent = set()

    async def notify_history_warning(level):
        if not level or level in history_warning_levels_sent:
            return
        history_warning_levels_sent.add(level)
        if level == "near_limit":
            warning_text = (
                "提醒：当前会话历史记录已接近系统容量上限。\n"
                "雾萌娘可能会在稍后自动归档较早的消息以保持体验顺畅。\n"
                "如果希望立即整理，可以使用 /clear 清空当前历史。"
            )
        elif level == "overflow":
            warning_text = (
                "提示：为了保证会话流畅，部分较早的聊天记录已被自动归档保存。\n"
                "当前对话不受影响，若需要查看完整历史请告诉雾萌娘。"
            )
        else:
            return

        await safe_send_markdown(
            partial_send(
                context.bot.send_message,
                chat_id=update.effective_chat.id,
            ),
            warning_text,
            logger=logger,
        )

    # 如果是媒体消息（图片或贴纸），固定硬币消耗3
    if effective_message.photo or effective_message.sticker:
        coin_cost = 3
        is_media = True
    else:
        # 保留原本文字消息长度判断逻辑
        user_message = effective_message.text
        if not user_message:
            logging.warning("收到没有文本内容的消息，忽略处理")
            return
        if len(user_message) > 4096:
            await effective_message.reply_text("消息过长，无法处理。请缩短消息长度！\nThe message is too long to process. Please shorten the message.")
            return
        elif len(user_message) > 1000:
            coin_cost = 3
        elif len(user_message) > 500:
            coin_cost = 2
        else:
            coin_cost = 1
        is_media = False

    # 异步方式获取并更新用户硬币
    connection = mysql_connection.create_connection()
    cursor = connection.cursor()
    try:
        select_query = "SELECT permission, coins FROM user WHERE id = %s"
        cursor.execute(select_query, (user_id,))
        result = cursor.fetchone()
        user_permission = result[0] if result else 0
        user_coins = result[1] if result else 0

        if user_coins < coin_cost:
            await effective_message.reply_text(
                f"您的硬币不足，无法与雾萌娘连接，需要{coin_cost}个硬币。试试通过 /lottery 抽奖吧！\n"
                f"You don't have enough coins (need {coin_cost}), I don't want to talk to you. "
                f"Try using /lottery to get some coins!")
            return

        update_query = "UPDATE user SET coins = coins - %s WHERE id = %s"
        cursor.execute(update_query, (coin_cost, user_id))
        connection.commit()
        user_coins = max(user_coins - coin_cost, 0)
    finally:
        cursor.close()
        connection.close()

    user_affection = await process_user.async_get_user_affection(user_id)
    user_impression_raw = await process_user.async_get_user_impression(user_id)
    impression_display = (user_impression_raw or "").strip()
    if impression_display:
        impression_display = impression_display.replace("\r", " ").replace("\n", " ")
        if len(impression_display) > 500:
            impression_display = impression_display[:497] + "..."
    else:
        impression_display = "未记录"

    if update.effective_chat.type in ("group", "supergroup"):
        group_title = (update.effective_chat.title or "").strip()
        if group_title:
            chat_type_label = f"Group: {group_title}"
        else:
            chat_type_label = "Group"
    else:
        chat_type_label = "Private"
    prefix = f"[{chat_type_label}] "

    # 如果是媒体消息，进行下载、AI分析、格式化描述
    if is_media:
        try:
            if effective_message.photo:
                media_type = "photo"
                file = await effective_message.photo[-1].get_file()
            else:
                media_type = "sticker"
                file = await effective_message.sticker.get_file()

            # 检查是否有文本说明
            caption = effective_message.caption if effective_message.caption else ""

            # 使用临时文件来存储
            with tempfile.NamedTemporaryFile(delete=False) as temp_file:
                file_path = temp_file.name
                await file.download_to_drive(file_path)

            # 读取文件转base64
            with open(file_path, 'rb') as f:
                file_bytes = f.read()
            base64_str = base64.b64encode(file_bytes).decode('utf-8')

            # 删除临时文件
            os.unlink(file_path)

            # 异步调用图像分析AI
            image_description = await ai_chat.analyze_image(base64_str)

            # 组合图片描述和用户文本说明
            if caption:
                formatted_message = f"""{prefix}{message_time} @{user_name} sent a {media_type} with caption: {caption}
                 
                Image description:
                {image_description}"""
            else:
                formatted_message = f"""{prefix}{message_time} @{user_name} sent a {media_type}. Description: {image_description}"""

        except Exception as e:
            logging.error(f"处理媒体消息时出错: {str(e)}")
            await effective_message.reply_text(
                "抱歉呢，雾萌娘暂时无法处理您发送的媒体，请稍后再试试看喵~\n"
                "Sorry, I'm having trouble processing your image/sticker right now. Please try again later, meow!")
            return
    else:
        # 保留原有文本处理逻辑，处理文本消息
        user_message = effective_message.text
        if effective_message.reply_to_message:
            quoted_message = effective_message.reply_to_message.text
            quoted_user = effective_message.reply_to_message.from_user.username or "EmptyUsername"  # 引用消息的用户名也需要处理
            formatted_message = f"""> Replying to @{quoted_user}: {quoted_message}
             
            {prefix}{message_time} @{user_name} said: {user_message}
            """
        else:
            formatted_message = f"{prefix}{message_time} @{user_name} said: {user_message}"

    # 异步获取聊天历史
    chat_history = await mysql_connection.async_get_chat_history(conversation_id)

    # 如果是新对话，添加个人信息
    if not chat_history:
        personal_info = await process_user.async_get_user_personal_info(user_id)
        if personal_info:
            personal_snapshot, personal_warning = await mysql_connection.async_insert_chat_record(conversation_id, 'user', personal_info)
            if personal_warning:
                await notify_history_warning(personal_warning)
            if personal_snapshot:
                summary.schedule_summary_generation(conversation_id)
            # 重新获取更新后的聊天历史
            chat_history = await mysql_connection.async_get_chat_history(conversation_id)

    # 异步插入用户消息
    user_snapshot_created, user_storage_warning = await mysql_connection.async_insert_chat_record(conversation_id, 'user', formatted_message)
    if user_storage_warning:
        await notify_history_warning(user_storage_warning)
    if user_snapshot_created:
        summary.schedule_summary_generation(conversation_id)
    
    # 立即获取最新历史记录，以便AI能看到刚刚插入的消息
    chat_history = await mysql_connection.async_get_chat_history(conversation_id)

    chat_history_for_ai = list(chat_history)

    # 异步发送"正在输入"状态
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    
    # 异步获取AI回复
    tool_context = {
        "is_group": update.effective_chat.type in ("group", "supergroup"),
        "group_id": update.effective_chat.id if update.effective_chat.type in ("group", "supergroup") else None,
        "message_id": getattr(effective_message, "message_id", None),
        "user_id": user_id,
        "user_state_prompt": 
        (f"""
# User Status
## Coins
 - **User's coins**: {user_coins}
 - User's consumption: 1 to 3 coins per message (system-managed)
 - Used for conversations and bot features (system handles this automatically)

## Permission Level
 - **User's permission**: {user_permission} 
 - Level: 0=Normal, 1=Advanced, 2=Maximum
 - Higher permission levels indicate wealthier users who can access more advanced features

## Affection Level
 - **Your affection towards them**: {user_affection}
 - Range: -100 to 100
 - Adjust your tone and attitude based on your affection level towards the user

## Impression
 - **Your impression of them**: {impression_display}
 - Record permanent user information such as occupation, interests, preferences, etc.
 - Help you better understand users and enhance the relevance of conversations 
        """),
    }

    assistant_message, tool_logs = await ai_chat.get_ai_response(chat_history_for_ai, user_id, tool_context=tool_context)

    pending_tool_call_ids = []
    for tool_log in tool_logs:
        entry_type = tool_log.get("type", "tool_result")
        tool_call_id = tool_log.get("tool_call_id")
        if not tool_call_id:
            if entry_type == "tool_result" and pending_tool_call_ids:
                tool_call_id = pending_tool_call_ids.pop(0)
            else:
                tool_call_id = f"auto_{int(time.time() * 1000)}"
        if entry_type == "assistant_tool_call":
            pending_tool_call_ids.append(tool_call_id)
            tool_log["tool_call_id"] = tool_call_id

        if entry_type == "assistant_tool_call":
            arguments = tool_log.get("arguments") or {}
            try:
                arguments_json = json.dumps(arguments, ensure_ascii=False)
            except TypeError:
                arguments_json = json.dumps({}, ensure_ascii=False)

            assistant_call_message = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": tool_call_id,
                        "type": "function",
                        "function": {
                            "name": tool_log.get("tool_name"),
                            "arguments": arguments_json,
                        },
                    }
                ],
            }

            tool_snapshot_created, tool_storage_warning = await mysql_connection.async_insert_chat_record(
                conversation_id,
                "assistant",
                assistant_call_message,
            )
        else:
            if pending_tool_call_ids and pending_tool_call_ids[0] == tool_call_id:
                pending_tool_call_ids.pop(0)
            tool_result = tool_log.get("result")
            try:
                tool_result_str = json.dumps(tool_result, ensure_ascii=False, default=str)
            except TypeError:
                tool_result_str = str(tool_result)

            tool_message = {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "name": tool_log.get("tool_name"),
                "content": tool_result_str,
            }

            tool_snapshot_created, tool_storage_warning = await mysql_connection.async_insert_chat_record(
                conversation_id,
                "tool",
                tool_message,
            )

        if tool_storage_warning:
            await notify_history_warning(tool_storage_warning)
        if tool_snapshot_created:
            summary.schedule_summary_generation(conversation_id)

    # 异步插入AI回复到聊天记录
    assistant_snapshot_created, assistant_storage_warning = await mysql_connection.async_insert_chat_record(conversation_id, 'assistant', assistant_message)
    if assistant_storage_warning:
        await notify_history_warning(assistant_storage_warning)
    if assistant_snapshot_created:
        summary.schedule_summary_generation(conversation_id)

    # 发送AI回复
    await safe_send_markdown(
        effective_message.reply_text,
        assistant_message,
        logger=logger,
        fallback_send=partial_send(
            context.bot.send_message,
            chat_id=update.effective_chat.id,
        ),
    )


last_rich_query_time = 0  # 新增：记录上次查询富豪榜的时间
@cooldown
async def rich_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_rich_query_time
    current_time = time.time()
    if current_time - last_rich_query_time < 60:
        await update.message.reply_text("查询过于频繁，每60秒只能查询一次，请稍后再试。")
        return
    last_rich_query_time = current_time
    connection = mysql_connection.create_connection()
    cursor = connection.cursor()
    try:
        query = "SELECT name, coins FROM user ORDER BY coins DESC LIMIT 5"
        cursor.execute(query)
        results = cursor.fetchall()
    except Exception as e:
        await update.message.reply_text(f"查询富豪榜时出错：{str(e)}")
        return
    finally:
        cursor.close()
        connection.close()
        
    if not results:
        await update.message.reply_text("暂无数据")
        return
        
    rich_list = " 富豪榜 Top 5 \n\n"
    for idx, (name, coins) in enumerate(results, start=1):
        rich_list += f"{idx}. {name} - {coins} 枚硬币\n"
    await update.message.reply_text(rich_list)


@cooldown
async def give_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /give <name> <num>
    赠送硬币：
    - name 为数据库表 user 中的 name 字段（目标用户）的值
    - num 为赠送的硬币数
    """
    if len(context.args) != 2:
        await update.message.reply_text("用法：/give <用户名> <数量>")
        return

    target_name = context.args[0]
    try:
        amount = int(context.args[1])
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("赠送数量必须为正整数！")
        return

    sender_id = update.effective_user.id

    # 连接数据库进行操作
    connection = mysql_connection.create_connection()
    cursor = connection.cursor()
    try:
        # 检查发送者是否存在，并获取硬币
        select_sender = "SELECT coins FROM user WHERE id = %s"
        cursor.execute(select_sender, (sender_id,))
        sender_data = cursor.fetchone()
        if not sender_data:
            await update.message.reply_text("请先使用 /me 命令注册个人信息。")
            return
        sender_coins = sender_data[0]
        if sender_coins < amount:
            await update.message.reply_text(f"您的硬币不足，当前硬币：{sender_coins}，需要：{amount}")
            return

        # 根据目标用户名查找接收者ID
        select_recipient = "SELECT id FROM user WHERE name = %s"
        cursor.execute(select_recipient, (target_name,))
        recipient_data = cursor.fetchone()
        if not recipient_data:
            await update.message.reply_text(f"未找到用户名为 '{target_name}' 的用户。")
            return
        recipient_id = recipient_data[0]

        if sender_id == recipient_id:
            await update.message.reply_text("不能给自己赠送硬币哦~")
            return

        # 开始转账操作：扣除发送者硬币，加到账户接收者
        update_sender = "UPDATE user SET coins = coins - %s WHERE id = %s"
        update_recipient = "UPDATE user SET coins = coins + %s WHERE id = %s"
        cursor.execute(update_sender, (amount, sender_id))
        cursor.execute(update_recipient, (amount, recipient_id))
        connection.commit()
        await update.message.reply_text(f"成功赠送 {amount} 枚硬币给用户 {target_name}。")
    except Exception as e:
        connection.rollback()
        await update.message.reply_text("转账过程中出现错误，请稍后再试。")
    finally:
        cursor.close()
        connection.close()


async def my_chat_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 当机器人的 chat member 状态更新时触发
    result = update.my_chat_member
    old_status = result.old_chat_member.status
    new_status = result.new_chat_member.status
    bot = await context.bot.get_me()
    # 判断更新是否为自己，并且状态从非成员变为成员或管理员
    if result.new_chat_member.user.id == bot.id and old_status in ["left", "kicked"] and new_status in ["member", "administrator", "creator"]:
        # 调用 /start 命令中的欢迎消息逻辑
        await start(update, context)


# 修改错误处理程序
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理Telegram API错误"""
    logging.error(f"Update {update} caused error {context.error}")
    
    # 根据不同类型的更新选择不同的回复方式
    try:
        if update and update.effective_message:
            await update.effective_message.reply_text(
                "看起来对话出现了一些小问题呢。"
                "您可以尝试使用 /clear 命令来清空聊天记录，"
                "然后我们重新开始对话吧！\n"
                "It seems there was a small issue with the conversation."
                "You can try using the  /clear  command to clear the chat history,"
                "and then we can start over!\n\n"
                "错误信息 Error message: \n\n" + str(context.error) + "\n\n您可以发送给管理员 @ScarletKc 报告此问题。\n"
                "You can report this issue to the admin @ScarletKc."
            )
        elif update and update.callback_query:
            # 对回调查询错误的处理
            await update.callback_query.answer("处理请求时出错，请稍后再试")
            if update.effective_chat:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="操作出错，请稍后再试。\n错误信息: " + str(context.error)
                )
    except Exception as e:
        logging.error(f"在处理错误时又发生了错误: {str(e)}")


@cooldown
async def tl_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """翻译命令处理函数"""
    # 获取用户ID以检查是否已注册
    user_id = update.effective_user.id
    if not await mysql_connection.async_check_user_exists(user_id):
        await update.message.reply_text(
            "请先使用 /me 命令注册个人信息后再使用翻译功能。\n"
            "Please register first using the /me command before using translation."
        )
        return
    
    text_to_translate = ""
    
    # 检查是否有回复消息
    if update.message.reply_to_message and update.message.reply_to_message.text:
        text_to_translate = update.message.reply_to_message.text
    # 检查是否有命令参数
    elif context.args:
        text_to_translate = " ".join(context.args)
    # 如果都没有，提示用法
    else:
        await update.message.reply_text(
            "使用方法：\n"
            "1. 回复一条消息并使用 /tl 命令\n"
            "2. 直接使用 /tl <文本> 进行翻译\n\n"
            "Usage:\n"
            "1. Reply to a message with /tl command\n"
            "2. Use /tl <text> to translate directly"
        )
        return
    
    # 如果文本过长，拒绝翻译
    if len(text_to_translate) > 3000:
        await update.message.reply_text(
            "文本太长，无法翻译。请尝试缩短文本。\n"
            "Text too long for translation. Please try with a shorter text."
        )
        return
    
    # 检查硬币是否足够（基于长度收费）
    coin_cost = 0
    if len(text_to_translate) > 500:
        coin_cost = 1
    if len(text_to_translate) > 1000:
        coin_cost = 2
    if len(text_to_translate) > 2000:
        coin_cost = 3
    
    # 获取用户硬币数
    user_coins = await process_user.async_get_user_coins(user_id)
    if user_coins < coin_cost:
        await update.message.reply_text(
            f"您的硬币不足，需要 {coin_cost} 枚硬币进行翻译。试试通过 /lottery 抽奖获取硬币吧！\n"
            f"You don't have enough coins (need {coin_cost}). Try using /lottery to get some coins!"
        )
        return
    
    # 扣除硬币
    connection = mysql_connection.create_connection()
    cursor = connection.cursor()
    try:
        update_query = "UPDATE user SET coins = coins - %s WHERE id = %s"
        cursor.execute(update_query, (coin_cost, user_id))
        connection.commit()
    finally:
        cursor.close()
        connection.close()
    
    # 不发送正在翻译状态
    # await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    
    # 调用翻译函数
    try:
        translation = await ai_chat.translate_text(text_to_translate)
        await update.message.reply_text(
            f"{translation}"
        )
    except Exception as e:
        logging.error(f"翻译出错: {str(e)}")
        await update.message.reply_text(
            "翻译服务暂时不可用，请稍后重试。\n"
            "Translation service is temporarily unavailable, please try again later. Your coins have been refunded."
        )
        # 退还硬币
        connection = mysql_connection.create_connection()
        cursor = connection.cursor()
        try:
            update_query = "UPDATE user SET coins = coins + %s WHERE id = %s"
            cursor.execute(update_query, (coin_cost, user_id))
            connection.commit()
        finally:
            cursor.close()
            connection.close()


if __name__ == '__main__':
    application = ApplicationBuilder() \
        .token(config.TELEGRAM_BOT_TOKEN) \
        .concurrent_updates(True) \
        .build()

    application.add_error_handler(error_handler)
    application.add_handler(CommandHandler("fogmoebot", reply))  # Call bot at group
    message_handler = MessageHandler(
        (filters.TEXT | filters.PHOTO | filters.Sticker.ALL) & 
        ~filters.COMMAND & 
        ~filters.VIA_BOT & 
        (filters.UpdateType.MESSAGE | filters.UpdateType.EDITED_MESSAGE),
        reply
    )
    application.add_handler(message_handler)
    start_handler = CommandHandler('start', start)
    application.add_handler(start_handler)
    me_handler = CommandHandler('me', me)
    application.add_handler(me_handler)
    lottery_handler = CommandHandler('lottery', lottery_command)
    application.add_handler(lottery_handler)
    help_handler = CommandHandler('help', help_command)
    application.add_handler(help_handler)
    github_handler = CommandHandler('github', github_command)
    application.add_handler(github_handler)
    clear_handler = CommandHandler('clear', clear_command)
    application.add_handler(clear_handler)
    admin_announce_handler = CommandHandler('admin_announce', admin_announce)
    application.add_handler(admin_announce_handler)
    setmyinfo_handler = CommandHandler('setmyinfo', setmyinfo_command)
    application.add_handler(setmyinfo_handler)
    give_handler = CommandHandler("give", give_command)
    application.add_handler(give_handler)
    bribe.setup_bribe_command(application)

    # 添加监控命令
    application.add_handler(CommandHandler("start_test_monitor", start_monitor))
    application.add_handler(CommandHandler("stop_test_monitor", stop_monitor))

    # 添加内联翻译处理程序（暂时禁用）
    # application.add_handler(InlineQueryHandler(inline_translate))

    # 添加赌博命令和回调处理
    application.add_handler(CommandHandler("gamble", gamble.gamble_command))
    application.add_handler(CallbackQueryHandler(gamble.gamble_callback, pattern=r"^gamble_"))

    #商店
    shop_handler = CommandHandler("shop", shop.shop_command)
    application.add_handler(shop_handler)
    application.add_handler(CallbackQueryHandler(shop.shop_callback, pattern=r"^shop_"))
    # 使用job_queue替代直接创建任务
    application.job_queue.run_repeating(shop.cleanup_message_records_job, interval=3600, first=10)

    #任务
    task_handler = CommandHandler("task", task.task_command)
    application.add_handler(task_handler)
    application.add_handler(CallbackQueryHandler(task.task_callback, pattern=r"^task_"))

    # 添加富豪榜指令
    rich_handler = CommandHandler("rich", rich_command)
    application.add_handler(rich_handler)

    # 注册 member_verify 模块的处理器
    member_verify.setup_member_verification(application)

    # 添加处理新群组成员的 handler
    application.add_handler(ChatMemberHandler(my_chat_member_handler, chat_member_types=ChatMemberHandler.MY_CHAT_MEMBER))
    
    # 添加质押系统处理器
    stake_coin.setup_stake_handlers(application)

    # 添加加密货币预测处理器
    crypto_predict.setup_crypto_predict_handlers(application)
    
    # 添加代币兑换处理器
    swap_fogmoe_solana_token.setup_swap_handler(application)

    # 添加翻译命令处理器
    tl_handler = CommandHandler('tl', tl_command)
    application.add_handler(tl_handler)

    # 添加关键词处理器
    keyword_handler.setup_keyword_handlers(application)
    
    # 添加垃圾信息过滤处理器
    spam_control.setup_spam_control_handlers(application)
    
    # 添加御神签模块处理器
    omikuji.setup_omikuji_handlers(application)
    
    # 添加石头剪刀布游戏处理器
    rockpaperscissors_game.setup_rps_game_handlers(application)
    
    # 添加充值系统处理器
    charge_coin.setup_charge_handlers(application)

    # 添加SICBO骰宝游戏处理器
    sicbo.setup_sicbo_handlers(application)

    # 注册推广系统的处理器
    ref.setup_ref_handlers(application)

    # 注册每日签到系统的处理器
    checkin.setup_checkin_handlers(application)

    # 注册举报系统的处理器
    report.setup_report_handlers(application)

    # 注册代币图表模块处理器
    chart.setup_chart_handlers(application)

    # 注册图片模块处理器
    pic.setup_pic_handlers(application)
    
    # 注册分享链接检测模块处理器 （暂时关闭）
    # sf.setup_sf_handlers(application)
    
    # 注册音乐搜索模块处理器
    music.setup_music_handlers(application)

    # 注册RPG游戏模块处理器
    application.add_handler(CommandHandler("rpg", rpg.rpg_command_handler))

    # 注册开发者命令模块处理器
    developer.setup_developer_handlers(application)

    # 注册Web密码模块处理器
    web_password.setup_webpassword_handlers(application)

    application.run_polling()
