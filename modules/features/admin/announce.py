import asyncio
import logging

import telegram
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from sqlalchemy.exc import SQLAlchemyError

from core import config, mysql_connection
from core.command_cooldown import cooldown

ADMIN_USER_ID = config.ADMIN_USER_ID


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

    try:
        users = await mysql_connection.fetch_all("SELECT id FROM user")
        user_ids.update(user[0] for user in users)

        group_tables = [
            "group_keywords",
            "group_verification",
            "group_spam_control",
            "group_chart_tokens",
            "chat_records_group",
        ]
        for table in group_tables:
            try:
                groups = await mysql_connection.fetch_all(f"SELECT DISTINCT group_id FROM {table}")
                group_ids.update(group[0] for group in groups)
            except SQLAlchemyError as table_err:
                logging.warning(f"查询群组表 {table} 时出错: {table_err}")
    except SQLAlchemyError as db_err:
        logging.error(f"数据库查询出错: {db_err}")
        await update.message.reply_text(f"数据库查询时出错: {db_err}")
        return

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
