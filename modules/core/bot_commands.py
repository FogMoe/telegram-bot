import asyncio
import json
import logging
import time
from uuid import uuid4

from telegram import InlineQueryResultArticle, InputTextMessageContent, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
import telegram
import mysql.connector

from core import config, mysql_connection, process_user
from core.command_cooldown import cooldown
from core.telegram_utils import safe_send_markdown, partial_send
from features.ai import ai_chat, summary
from features.economy import ref

logger = logging.getLogger(__name__)

ADMIN_USER_ID = config.ADMIN_USER_ID
last_rich_query_time = 0


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
            update.effective_chat.id,
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
            update.effective_chat.id,
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
