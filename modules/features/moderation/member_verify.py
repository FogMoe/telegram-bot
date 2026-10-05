import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
from telegram.constants import ChatMemberStatus
from telegram.ext import ContextTypes, CommandHandler, CallbackQueryHandler, MessageHandler, filters
from core import mysql_connection
from datetime import datetime, timedelta
from core.command_cooldown import cooldown
from core.redaction import report_error

# 新成员的验证时限（秒）
VERIFY_SECONDS = 300
# 恢复任务的周期：重启后第一次在 RECOVERY_FIRST_SECONDS 秒后运行，之后每隔 RECOVERY_INTERVAL 秒。
RECOVERY_INTERVAL = 30
RECOVERY_FIRST_SECONDS = 5

# verification_tasks 每个 (用户, 群组) 一行，记录当前这一轮验证的欢迎消息。
# 验证按钮只对这条消息有效：重新入群会换成新消息，旧消息上的按钮和旧定时器随之失效。

async def save_verification_task(user_id, chat_id, message_id, expire_time):
    await mysql_connection.execute(
        "INSERT INTO verification_tasks (user_id, group_id, message_id, expire_time) "
        "VALUES (%s, %s, %s, %s) "
        "ON DUPLICATE KEY UPDATE message_id = VALUES(message_id), expire_time = VALUES(expire_time)",
        (user_id, chat_id, message_id, expire_time),
    )

# 认领某一轮验证：删掉这一轮的记录，删到的一方才继续处理，
# 所以按钮点击、超时和离群对同一轮只会有一个生效
async def claim_verification_task(user_id, chat_id, message_id):
    deleted = await mysql_connection.execute(
        "DELETE FROM verification_tasks WHERE user_id = %s AND group_id = %s AND message_id = %s",
        (user_id, chat_id, message_id),
    )
    return deleted > 0

async def find_verification_message(user_id, chat_id):
    row = await mysql_connection.fetch_one(
        "SELECT message_id FROM verification_tasks WHERE user_id = %s AND group_id = %s",
        (user_id, chat_id),
    )
    return row[0] if row else None

async def due_verification_tasks(now):
    return await mysql_connection.fetch_all(
        "SELECT user_id, group_id, message_id FROM verification_tasks WHERE expire_time <= %s",
        (now,),
    )

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
    record = await mysql_connection.fetch_one(
        "SELECT group_id FROM group_verification WHERE group_id = %s",
        (chat_id,),
    )

    if record:
        # 若记录存在，则只有群组管理员才能取消接管
        sender_member = await context.bot.get_chat_member(chat_id, update.effective_user.id)
        if sender_member.status not in ["administrator", "creator"]:
            await update.message.reply_text("只有群组管理员才能取消接管。")
            return
        context.chat_data["enable_verify"] = False
        await mysql_connection.execute(
            "DELETE FROM group_verification WHERE group_id = %s",
            (chat_id,),
        )
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
    insert_query = (
        "INSERT INTO group_verification (group_id, group_name) "
        "VALUES (%s, %s) "
        "ON DUPLICATE KEY UPDATE group_name = VALUES(group_name)"
    )
    group_name = update.effective_chat.title if update.effective_chat.title else "未知群组"
    await mysql_connection.execute(insert_query, (chat_id, group_name))
    await update.message.reply_text("新成员验证功能已开启。新成员加入时将被禁言并要求点击【验证】按钮验证，5分钟内有效。")

# 新成员加入事件处理
async def new_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    
    # 从数据库直接查询群组是否开启了验证功能
    record = await mysql_connection.fetch_one(
        "SELECT group_id FROM group_verification WHERE group_id = %s",
        (chat_id,),
    )
    verification_enabled = record is not None
    
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
            notice = report_error(
                logging.getLogger(__name__), f"限制成员 {user_id} 失败", e
            )
            if "httpx.ConnectError" in error_str or "Not enough rights" in error_str:
                await context.bot.send_message(
                    chat_id,
                    f"验证错误: 无法限制成员 {new_member.full_name}({user_id})，"
                    f"请检查机器人的管理员权限与网络。\n{notice}"
                )
            continue

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("点击验证", callback_data=f"verify_{user_id}")]
        ])

        # 发送欢迎信息，包含验证按钮
        welcome_msg = await update.message.reply_text(
            f"欢迎 {new_member.mention_html()} 加入群组！请点击【验证】按钮进行验证（5分钟内有效）。",
            reply_markup=keyboard,
            parse_mode="HTML"
        )
        await save_verification_task(
            user_id,
            chat_id,
            welcome_msg.message_id,
            datetime.now() + timedelta(seconds=VERIFY_SECONDS),
        )
        # 到点处理超时；进程重启会丢掉这个定时器，由 recover_verification_tasks 兜底
        context.job_queue.run_once(
            verification_timeout_job,
            when=VERIFY_SECONDS + 0.5,
            data=(chat_id, user_id, welcome_msg.message_id),
        )

# 某一轮验证到期仍未通过：移出群组并编辑欢迎消息
async def expire_verification(bot, chat_id, user_id, message_id):
    # 已验证、已离群或已被新一轮取代时，这一轮的记录已经不在
    if not await claim_verification_task(user_id, chat_id, message_id):
        return
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except Exception as e:
        print(f"查询成员 {user_id} 状态时出错: {e}")
        return
    # 只移出仍被禁言的成员，管理员已经手动放行的不动
    if member.status != ChatMemberStatus.RESTRICTED or member.can_send_messages:
        return
    try:
        # 先封禁再立即解禁：只移出，不永久封禁
        await bot.ban_chat_member(chat_id, user_id)
        await bot.unban_chat_member(chat_id, user_id)
    except Exception as e:
        print(f"踢出成员 {user_id} 时出错: {e}")
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text="验证超时，您已被移出群组。"
        )
    except Exception as e:
        print(f"编辑消息 {message_id} 出错: {e}")

async def verification_timeout_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id, user_id, message_id = context.job.data
    await expire_verification(context.bot, chat_id, user_id, message_id)

async def recover_verification_tasks(context: ContextTypes.DEFAULT_TYPE):
    """启动后与周期性的恢复：处理已经到期、定时器却随进程重启丢失的验证。"""
    for user_id, chat_id, message_id in await due_verification_tasks(datetime.now()):
        try:
            await expire_verification(context.bot, chat_id, user_id, message_id)
        except Exception:
            logging.getLogger(__name__).exception("处理群组 %s 成员 %s 的过期验证失败", chat_id, user_id)

# 按钮是否属于点击者；兼容旧版本发出的 verify_<用户>_<令牌> 按钮
def is_own_verify_button(callback_data, user_id):
    callback_parts = callback_data.split("_")
    return len(callback_parts) in (2, 3) and callback_parts[0] == "verify" and callback_parts[1] == str(user_id)

# 回调查询处理：点击验证按钮时解除禁言并更新消息
async def verify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = update.effective_chat.id

    if not is_own_verify_button(query.data, user_id):
        await query.answer("这不是为您准备的验证按钮。", show_alert=True)
        return

    message_id = query.message.message_id if query.message else None
    if await claim_verification_task(user_id, chat_id, message_id):
        try:
            # 解除禁言（恢复发送消息权限）
            await context.bot.restrict_chat_member(
                chat_id,
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
        except Exception as e:
            # 放回这一轮的记录：用户可以再点一次，一直没通过的照常按超时处理
            await save_verification_task(
                user_id, chat_id, message_id, datetime.now() + timedelta(seconds=VERIFY_SECONDS)
            )
            error_str = str(e)
            notice = report_error(
                logging.getLogger(__name__), f"解除成员 {user_id} 禁言失败", e
            )
            if "httpx.ConnectError" in error_str or "Not enough rights" in error_str:
                await context.bot.send_message(
                    chat_id,
                    f"验证错误: 无法解除禁言成员({user_id})，"
                    f"请检查机器人的管理员权限与网络。\n{notice}"
                )
            await query.answer("验证时出现错误，请稍后再试。", show_alert=True)
    else:
        await query.answer("验证已失效或已处理。", show_alert=True)
        try:
            # 删除验证消息
            await query.delete_message()
        except Exception as e:
            print(f"删除验证消息时出错: {e}")

# 处理成员离开群组的事件（合并处理机器人和普通用户）
async def handle_member_left(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.message.left_chat_member
    bot = await context.bot.get_me()

    # 如果是机器人自己被踢出
    if user.id == bot.id:
        # 清理数据库中的验证配置和未完成的验证
        await mysql_connection.execute(
            "DELETE FROM group_verification WHERE group_id = %s",
            (chat_id,),
        )
        await mysql_connection.execute(
            "DELETE FROM verification_tasks WHERE group_id = %s",
            (chat_id,),
        )
        return

    # 如果是普通成员离开，结束他未完成的验证
    message_id = await find_verification_message(user.id, chat_id)
    if message_id is not None and await claim_verification_task(user.id, chat_id, message_id):
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"用户 {user.full_name} 在验证前离开了群组。"
            )
        except Exception as e:
            print(f"编辑消息出错: {e}")

# 注册该模块的处理器
def setup_member_verification(dispatcher):
    dispatcher.add_handler(CommandHandler("verify", verify_command))
    dispatcher.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, new_member_handler))
    dispatcher.add_handler(CallbackQueryHandler(verify_callback, pattern=r"^verify_"))
    dispatcher.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, handle_member_left))
    dispatcher.job_queue.run_repeating(
        recover_verification_tasks, interval=RECOVERY_INTERVAL, first=RECOVERY_FIRST_SECONDS
    )
