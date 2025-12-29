import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
from telegram.ext import ContextTypes, CommandHandler, CallbackQueryHandler, MessageHandler, filters
from core import mysql_connection
from datetime import datetime, timedelta
import secrets
from core.command_cooldown import cooldown

# 在开启验证功能前详细检查必要权限
async def check_bot_permissions(bot, chat_id):
    bot_member = await bot.get_chat_member(chat_id, bot.id)
    if (bot_member.status not in ["administrator", "creator"]):
        return False, "机器人需要管理员权限"
    
    # 检查具体权限
    required_permissions = {
        "can_restrict_members": "限制成员",
    }
    
    missing_permissions = []
    for perm, desc in required_permissions.items():
        if not getattr(bot_member, perm, False):
            missing_permissions.append(desc)
    
    if missing_permissions:
        return False, f"机器人缺少以下权限: {', '.join(missing_permissions)}"
    
    return True, "权限检查通过"

# /verify 命令：开启新成员验证功能
@cooldown
async def verify_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    # 查询数据库判断当前群组是否已开启接管验证
    conn = mysql_connection.create_connection()
    cursor = conn.cursor()
    try:
        select_query = "SELECT group_id FROM group_verification WHERE group_id = %s"
        cursor.execute(select_query, (chat_id,))
        record = cursor.fetchone()
    finally:
        cursor.close()
        conn.close()

    if record:
        # 若记录存在，则只有群组管理员才能取消接管
        sender_member = await context.bot.get_chat_member(chat_id, update.effective_user.id)
        if sender_member.status not in ["administrator", "creator"]:
            await update.message.reply_text("只有群组管理员才能取消接管。")
            return
        context.chat_data["enable_verify"] = False
        conn = mysql_connection.create_connection()
        cursor = conn.cursor()
        try:
            delete_query = "DELETE FROM group_verification WHERE group_id = %s"
            cursor.execute(delete_query, (chat_id,))
            conn.commit()
        finally:
            cursor.close()
            conn.close()
        await update.message.reply_text("验证接管已取消。")
        return

    # 仅允许群组管理员调用
    sender_member = await context.bot.get_chat_member(chat_id, update.effective_user.id)
    if sender_member.status not in ["administrator", "creator"]:
        await update.message.reply_text("只有群组管理员才能使用该命令。")
        return
    # 检查机器人是否具备管理员权限
    has_permissions, message = await check_bot_permissions(context.bot, chat_id)
    if not has_permissions:
        await update.message.reply_text(f"机器人缺少必要权限，无法开启验证功能：{message}")
        return
    # 启用接管验证，并将当前群组信息存储到数据库中
    context.chat_data["enable_verify"] = True
    conn = mysql_connection.create_connection()
    cursor = conn.cursor()
    try:
        insert_query = ("INSERT INTO group_verification (group_id, group_name) "
                        "VALUES (%s, %s) "
                        "ON DUPLICATE KEY UPDATE group_name = VALUES(group_name)")
        group_name = update.effective_chat.title if update.effective_chat.title else "未知群组"
        cursor.execute(insert_query, (chat_id, group_name))
        conn.commit()
    finally:
        cursor.close()
        conn.close()
    await update.message.reply_text("新成员验证功能已开启。新成员加入时将被禁言并要求点击【验证】按钮验证，5分钟内有效。")

# 新成员加入事件处理
async def new_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    
    # 从数据库直接查询群组是否开启了验证功能
    conn = mysql_connection.create_connection()
    cursor = conn.cursor()
    try:
        select_query = "SELECT group_id FROM group_verification WHERE group_id = %s"
        cursor.execute(select_query, (chat_id,))
        record = cursor.fetchone()
        verification_enabled = record is not None
    finally:
        cursor.close()
        conn.close()
    
    # 若未开启验证功能，则直接返回
    if not verification_enabled:
        return
    
    # 同步内存状态变量（可选，为了保持一致性）
    context.chat_data["enable_verify"] = True
    
    for new_member in update.message.new_chat_members:
        user_id = new_member.id
        
        # 跳过机器人验证
        if new_member.is_bot:
            print(f"跳过机器人 {new_member.full_name}({user_id}) 的验证")
            continue
            
        try:
            # 禁言新成员（禁止发送消息）
            await context.bot.restrict_chat_member(
                chat_id,
                user_id,
                ChatPermissions(can_send_messages=False,
                                can_send_polls=False,
                                can_send_other_messages=False,
                                can_add_web_page_previews=False,
                                can_change_info=False,
                                can_invite_users=False,
                                can_pin_messages=False,
                                can_manage_topics=False,
                                can_send_audios=False,
                                can_send_documents=False,
                                can_send_photos=False,
                                can_send_videos=False,
                                can_send_video_notes=False,
                                can_send_voice_notes=False)
            )
        except Exception as e:
            error_str = str(e)
            print(f"限制成员 {user_id} 失败: {error_str}")
            if "httpx.ConnectError" in error_str or "Not enough rights" in error_str:
                await context.bot.send_message(
                    chat_id,
                    f"验证错误: 无法限制成员 {new_member.full_name}({user_id})：{error_str}"
                )
            continue

        # 生成验证令牌
        token = secrets.token_hex(8)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("点击验证", callback_data=f"verify_{user_id}_{token}")]
        ])

        # 发送欢迎信息，包含验证按钮
        welcome_msg = await update.message.reply_text(
            f"欢迎 {new_member.mention_html()} 加入群组！请点击【验证】按钮进行验证（5分钟内有效）。",
            reply_markup=keyboard,
            parse_mode="HTML"
        )
        # 保存任务信息：包含欢迎消息ID及定时任务
        if "verify_tasks" not in context.chat_data:
            context.chat_data["verify_tasks"] = {}
        context.chat_data["verify_tasks"][user_id] = {
            "message_id": welcome_msg.message_id,
            "timer": asyncio.create_task(verification_timeout(context, chat_id, user_id, welcome_msg.message_id))
        }

        # 在发送欢迎信息后保存验证任务到数据库
        expire_time = datetime.now() + timedelta(minutes=5)
        conn = mysql_connection.create_connection()
        cursor = conn.cursor()
        try:
            insert_query = """
            INSERT INTO verification_tasks (user_id, group_id, message_id, expire_time) 
            VALUES (%s, %s, %s, %s) 
            ON DUPLICATE KEY UPDATE message_id = VALUES(message_id), expire_time = VALUES(expire_time)
            """
            cursor.execute(insert_query, (user_id, chat_id, welcome_msg.message_id, expire_time))
            conn.commit()
        finally:
            cursor.close()
            conn.close()

# 定时任务：等待5分钟后若未验证，则移出群组并编辑欢迎消息
async def verification_timeout(context: ContextTypes.DEFAULT_TYPE, chat_id, user_id, message_id):
    await asyncio.sleep(300)  # 等待5分钟
    verify_tasks = context.chat_data.get("verify_tasks", {})
    task_info = verify_tasks.get(user_id)
    if task_info:
        try:
            # 将 kick_chat_member 替换为 ban_chat_member
            await context.bot.ban_chat_member(chat_id, user_id)
            # 可选择解禁以便记录：这里立即解禁防止永久封禁
            await context.bot.unban_chat_member(chat_id, user_id)
        except Exception as e:
            print(f"踢出成员 {user_id} 时出错: {e}")
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text="验证超时，您已被移出群组。"
            )
        except Exception as e:
            print(f"编辑消息 {message_id} 出错: {e}")
        # 清除任务记录
        verify_tasks.pop(user_id, None)
        # 在清除任务记录时同时从数据库删除
        conn = mysql_connection.create_connection()
        cursor = conn.cursor()
        try:
            delete_query = "DELETE FROM verification_tasks WHERE user_id = %s AND group_id = %s"
            cursor.execute(delete_query, (user_id, chat_id))
            conn.commit()
        finally:
            cursor.close()
            conn.close()

# 回调查询处理：点击验证按钮时解除禁言并更新消息
async def verify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    
    # 解析回调数据
    callback_parts = query.data.split("_")
    if len(callback_parts) != 3 or callback_parts[0] != "verify" or callback_parts[1] != str(user_id):
        await query.answer("这不是为您准备的验证按钮。", show_alert=True)
        return
    
    verify_tasks = context.chat_data.get("verify_tasks", {})
    task_info = verify_tasks.get(user_id)
    if task_info:
        timer_task = task_info.get("timer")
        if timer_task and not timer_task.done():
            timer_task.cancel()
        message_id = task_info["message_id"]
        verify_tasks.pop(user_id, None)
        try:
            # 解除禁言（恢复发送消息权限）
            await context.bot.restrict_chat_member(
                update.effective_chat.id,
                user_id,
                ChatPermissions(can_send_messages=True,
                                can_send_polls=True,
                                can_send_other_messages=True,
                                can_add_web_page_previews=True,
                                can_change_info=True,
                                can_invite_users=True,
                                can_pin_messages=True,
                                can_manage_topics=True,
                                can_send_audios=True,
                                can_send_documents=True,
                                can_send_photos=True,
                                can_send_videos=True,
                                can_send_video_notes=True,
                                can_send_voice_notes=True,)
            )
            await query.edit_message_text("验证通过，欢迎加入群组！")
            await query.answer("验证成功！", show_alert=True)
            
            # 从数据库中删除验证任务记录
            conn = mysql_connection.create_connection()
            cursor = conn.cursor()
            try:
                delete_query = "DELETE FROM verification_tasks WHERE user_id = %s AND group_id = %s"
                cursor.execute(delete_query, (user_id, update.effective_chat.id))
                conn.commit()
            finally:
                cursor.close()
                conn.close()
        except Exception as e:
            error_str = str(e)
            if "httpx.ConnectError" in error_str or "Not enough rights" in error_str:
                await context.bot.send_message(
                    update.effective_chat.id,
                    f"验证错误: 无法解除禁言成员({user_id})：{error_str}"
                )
            await query.answer("验证时出现错误，请稍后再试。", show_alert=True)
    else:
        await query.answer("验证已失效或已处理。", show_alert=True)
        try:
            # 删除验证消息
            await query.delete_message()
        except Exception as e:
            print(f"删除验证消息时出错: {e}")

# 在启动时恢复验证任务
async def restore_verification_tasks(dispatcher):
    """从数据库恢复所有未完成的验证任务"""
    conn = mysql_connection.create_connection()
    cursor = conn.cursor()
    try:
        # 查询未过期的验证任务
        now = datetime.now()
        cursor.execute(
            "SELECT user_id, group_id, message_id, expire_time FROM verification_tasks WHERE expire_time > %s",
            (now,)
        )
        tasks = cursor.fetchall()
        
        for user_id, chat_id, message_id, expire_time in tasks:
            # 计算剩余时间
            remaining_time = (expire_time - now).total_seconds()
            if (remaining_time > 0):
                # 重建超时任务
                asyncio.create_task(
                    verification_timeout(dispatcher.application, chat_id, user_id, message_id)
                )
    finally:
        cursor.close()
        conn.close()

# 处理成员离开群组的事件（合并处理机器人和普通用户）
async def handle_member_left(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.message.left_chat_member
    bot = await context.bot.get_me()
    
    # 如果是机器人自己被踢出
    if user.id == bot.id:
        # 清理数据库中的验证配置
        conn = mysql_connection.create_connection()
        cursor = conn.cursor()
        try:
            delete_query = "DELETE FROM group_verification WHERE group_id = %s"
            cursor.execute(delete_query, (chat_id,))
            conn.commit()
        finally:
            cursor.close()
            conn.close()
        return
    
    # 如果是普通成员离开，检查是否有未完成的验证任务
    user_id = user.id
    verify_tasks = context.chat_data.get("verify_tasks", {})
    task_info = verify_tasks.get(user_id)
    
    if task_info:
        # 取消定时任务
        timer_task = task_info.get("timer")
        if timer_task and not timer_task.done():
            timer_task.cancel()
            
        # 尝试编辑欢迎消息
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=task_info["message_id"],
                text=f"用户 {user.full_name} 在验证前离开了群组。"
            )
        except Exception as e:
            print(f"编辑消息出错: {e}")
            
        # 从内存中删除验证任务
        verify_tasks.pop(user_id, None)
        
        # 从数据库中删除验证任务
        conn = mysql_connection.create_connection()
        cursor = conn.cursor()
        try:
            delete_query = "DELETE FROM verification_tasks WHERE user_id = %s AND group_id = %s"
            cursor.execute(delete_query, (user_id, chat_id))
            conn.commit()
        finally:
            cursor.close()
            conn.close()

# 注册该模块的处理器
def setup_member_verification(dispatcher):
    dispatcher.add_handler(CommandHandler("verify", verify_command))
    dispatcher.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, new_member_handler))
    dispatcher.add_handler(CallbackQueryHandler(verify_callback, pattern=r"^verify_"))
    dispatcher.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, handle_member_left))
