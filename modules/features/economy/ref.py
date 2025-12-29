import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, CommandHandler, CallbackQueryHandler
from mysql.connector import Error

from core import mysql_connection, process_user
import asyncio
from concurrent.futures import ThreadPoolExecutor
from core.command_cooldown import cooldown 

# 创建线程池执行器用于异步数据库操作
ref_executor = ThreadPoolExecutor(max_workers=5)

# 用于存储正在处理的邀请记录，防止重复处理
processing_invitations = set()

# 配置logger
logger = logging.getLogger(__name__)

# 邀请奖励的金币数量
INVITATION_REWARD = 20
# GROUP_REWARD = 20
# MIN_GROUP_MEMBERS = 20

async def process_start_with_args(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理带参数的/start命令，用于推广系统的邀请链接"""
    user_id = update.effective_user.id
    user_name = update.effective_user.full_name
    
    # 获取启动参数（邀请人ID）
    try:
        referrer_id = int(context.args[0])
    except (ValueError, IndexError):
        return False
    
    # 检查是否是自己邀请自己
    if user_id == referrer_id:
        return False
    
    # 添加邀请记录，并给双方发放奖励
    success = await async_add_invitation_record(user_id, referrer_id, user_name)
    if success:
        try:
            # 获取邀请人的用户名
            referrer_name = await async_get_user_name(referrer_id)
            
            # 获取邀请人的Telegram用户名（如果可能）
            try:
                # 尝试直接获取用户信息
                chat = await context.bot.get_chat(referrer_id)
                if chat and chat.username:
                    referrer_display = f"@{chat.username}"
                elif referrer_name:
                    referrer_display = f"{referrer_name} (`{referrer_id}`)"
                else:
                    referrer_display = f"`{referrer_id}`"
            except Exception as e:
                # 如果无法获取Telegram用户信息，使用数据库中的名称
                logger.error(f"Error getting chat for user {referrer_id}: {e}")
                if referrer_name:
                    referrer_display = f"{referrer_name} (`{referrer_id}`)"
                else:
                    referrer_display = f"`{referrer_id}`"
            
            # 向被邀请用户发送欢迎消息，使用Markdown格式
            await update.message.reply_text(
                f"🎁 您已通过邀请链接加入，获得了 *{INVITATION_REWARD}* 金币奖励！\n"
                f"您的邀请人是：{referrer_display}",
                parse_mode=ParseMode.MARKDOWN
            )
            return True
        except Exception as e:
            logger.error(f"Error in process_start_with_args when sending message: {e}")
            # 如果获取用户名或发送消息失败，使用原始ID
            await update.message.reply_text(
                f"🎁 您已通过邀请链接加入，获得了 *{INVITATION_REWARD}* 金币奖励！\n"
                f"您的邀请人是：`{referrer_id}`",
                parse_mode=ParseMode.MARKDOWN
            )
            return True
    return False

@cooldown
async def ref_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理/ref命令，根据是否有参数执行不同的功能"""
    if not context.args:
        # 没有参数，显示用户的邀请信息
        try:
            user_id = update.effective_user.id
            user_name = update.effective_user.full_name
            # 从数据库获取该用户邀请的信息
            invited_count, invited_users = await async_get_invited_users(user_id)
            
            # 获取当前用户的邀请人信息
            referrer_info = await async_get_referrer(user_id)
            
            # 生成邀请链接
            bot_username = (await context.bot.get_me()).username
            invite_link = f"https://t.me/{bot_username}?start={user_id}"
            
            # 准备回复消息，使用Markdown格式
            message = (
                f"🎉 *您的邀请信息* 🎉\n\n"
                f"📊 已邀请人数：*{invited_count}*\n"
                f"💰 已获得奖励：*{invited_count * INVITATION_REWARD}* 金币\n\n"
            )
            
            # 如果有邀请人，显示邀请人信息
            if referrer_info:
                referrer_id, referrer_name = referrer_info
                message += f"👤 您的邀请人：*{referrer_name}* (`{referrer_id}`)\n\n"
            
            message += (
                f"您的邀请码：`{user_id}`\n\n"  # 使用代码块格式，方便用户点击复制
                f"🔗 您的专属邀请链接：\n`{invite_link}`\n\n"  # 使用代码块格式，方便用户点击复制
                f"将此链接分享给好友，当他们点击链接并启动机器人时，您将获得 *{INVITATION_REWARD}* 金币奖励！\n\n"
                f"✨ *邀请规则：*\n"
                f"- 每邀请一位新用户，您将获得 *{INVITATION_REWARD}* 金币奖励\n"
                f"- 被邀请用户也将获得 *{INVITATION_REWARD}* 金币奖励\n"
                f"- 每个Telegram账号只能被邀请一次\n\n"
                # f"- 将机器人添加到 *{MIN_GROUP_MEMBERS}* 人以上的群组，可获得 *{GROUP_REWARD}* 金币奖励\n\n"
                f"如需手动绑定邀请人，请使用命令：`/ref <邀请码>`\n"
                f"例如：`/ref {user_id}`"  # 使用用户自己的ID作为示例
            )
            
            # 如果有邀请的用户，列出前10个
            if invited_users:
                message += "\n\n🙋‍♂️ *最近邀请的用户（最多显示10个）：*\n"
                for idx, (invited_id, invited_name, invitation_time) in enumerate(invited_users[:10], 1):
                    message += f"{idx}. {invited_name} (`{invited_id}`) - {invitation_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            
            await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            logger.error(f"Error in ref_command (show info): {e}")
            await update.message.reply_text("获取邀请信息时出错，请稍后再试。")
        return
    
    # 有参数，执行绑定邀请人功能
    try:
        referrer_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("邀请码必须是数字！")
        return
    
    try:
        # 检查是否是自己邀请自己
        if update.effective_user.id == referrer_id:
            await update.message.reply_text("您不能邀请自己哦！")
            return
        
        # 检查用户是否已经被邀请过
        user_id = update.effective_user.id
        current_referrer = await async_get_referrer(user_id)
        if current_referrer:
            referrer_id_db, referrer_name = current_referrer
            await update.message.reply_text(
                f"绑定失败，您已经被 *{referrer_name}* (`{referrer_id_db}`) 邀请过了。每个用户只能被邀请一次。",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        # 添加邀请记录，并给双方发放奖励
        success = await async_add_invitation_record(user_id, referrer_id, update.effective_user.full_name)
        if success:
            await update.message.reply_text(f"邀请绑定成功！您获得了 *{INVITATION_REWARD}* 金币奖励！", parse_mode=ParseMode.MARKDOWN)
        else:
            # 检查邀请人是否存在
            referrer_exists = await async_check_user_exists(referrer_id)
            if not referrer_exists:
                await update.message.reply_text("邀请绑定失败，邀请人不存在。请检查邀请码是否正确。")
            else:
                await update.message.reply_text("邀请绑定失败，可能是系统错误。请稍后再试。")
    except Exception as e:
        logger.error(f"Error in ref_command (bind referrer): {e}")
        await update.message.reply_text("处理邀请绑定时出错，请稍后再试。")

async def ref_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理推广系统的按钮回调"""
    query = update.callback_query
    await query.answer()
    
    # 因为移除了复制邀请链接按钮，此函数可以保留以备将来扩展，但目前不做任何操作
    pass

# async def handle_new_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     """处理机器人被添加到新群组的事件"""
#     # 添加日志检查函数是否被调用
#     logger.info(f"handle_new_chat_member called")
#     
#     # 只处理机器人被添加到群组的事件
#     if update.my_chat_member and update.my_chat_member.new_chat_member.user.id == context.bot.id:
#         chat = update.effective_chat
#         
#         # 获取添加机器人的用户信息
#         # 首先检查effective_user
#         user = update.effective_user
#         
#         # 如果effective_user为None，则从my_chat_member.from_user获取
#         if user is None and hasattr(update.my_chat_member, 'from_user'):
#             user = update.my_chat_member.from_user
#             logger.info(f"Using my_chat_member.from_user: {user.id} ({user.full_name})")
#         
#         if user is None:
#             logger.error("无法确定谁添加了机器人到群组，无法发放奖励")
#             return
#         
#         logger.info(f"Bot added to group: {chat.title} (ID: {chat.id}) by user: {user.full_name} (ID: {user.id})")
#         
#         # 检查群组成员数量
#         try:
#             chat_member_count = await context.bot.get_chat_member_count(chat.id)
#             logger.info(f"Group {chat.title} has {chat_member_count} members")
#             
#             # 先在群组中发送一条欢迎消息，这样即使数据库操作失败也能给用户反馈
#             try:
#                 await context.bot.send_message(
#                     chat_id=chat.id,
#                     text=f"感谢 {user.full_name} 将我添加到这个群组！\n"
#                          f"群组成员数: {chat_member_count}/{MIN_GROUP_MEMBERS}"
#                 )
#             except Exception as e:
#                 logger.error(f"Failed to send welcome message to group: {e}")
#             
#             # 确保用户存在于user表中
#             user_exists = await async_check_user_exists(user.id)
#             if not user_exists:
#                 logger.info(f"Creating new user record for {user.id}")
#                 await process_user.async_add_user(user.id, user.full_name, 0)
#             
#             # 记录机器人被添加到群组的信息
#             success = await async_record_group_addition(user.id, chat.id, chat.title, chat_member_count)
#             logger.info(f"Record group addition result: {success}")
#             
#             if success and chat_member_count >= MIN_GROUP_MEMBERS:
#                 # 私聊通知用户获得奖励
#                 try:
#                     logger.info(f"Sending reward notification to user {user.id}")
#                     await context.bot.send_message(
#                         chat_id=user.id,
#                         text=f"感谢您将机器人添加到群组 '{chat.title}'！\n"
#                              f"由于该群组成员数量达到{MIN_GROUP_MEMBERS}人以上，您获得了{GROUP_REWARD}金币奖励！"
#                     )
#                 except Exception as e:
#                     logger.error(f"Failed to send reward message to user {user.id}: {e}")
#             elif chat_member_count < MIN_GROUP_MEMBERS:
#                 logger.info(f"Group {chat.title} has only {chat_member_count} members, no reward given (minimum required: {MIN_GROUP_MEMBERS})")
#                 try:
#                     await context.bot.send_message(
#                         chat_id=user.id,
#                         text=f"感谢您将机器人添加到群组 '{chat.title}'！\n"
#                              f"目前该群组成员数量为{chat_member_count}人，未达到{MIN_GROUP_MEMBERS}人，暂时没有获得奖励。\n"
#                              f"当群组成员数量达到{MIN_GROUP_MEMBERS}人以上时，您将自动获得{GROUP_REWARD}金币奖励！"
#                     )
#                 except Exception as e:
#                     logger.error(f"Failed to send insufficient members message to user {user.id}: {e}")
#         except Exception as e:
#             logger.error(f"Error handling new chat member: {e}")
#             import traceback
#             logger.error(traceback.format_exc())

# 数据库操作函数
def add_invitation_record(invited_user_id, referrer_id, invited_user_name):
    """添加邀请记录到数据库，并给邀请人和被邀请人发放奖励"""
    # 如果此邀请组合正在处理中，则跳过
    invitation_key = f"{invited_user_id}_{referrer_id}"
    if invitation_key in processing_invitations:
        return False
    
    processing_invitations.add(invitation_key)
    connection = None
    cursor = None
    
    try:
        connection = mysql_connection.create_connection()
        if not connection:
            logger.error("Failed to create database connection in add_invitation_record")
            processing_invitations.remove(invitation_key)
            return False
            
        cursor = connection.cursor()
        
        # 检查被邀请用户是否已经有邀请记录
        cursor.execute("SELECT referrer_id FROM user_invitations WHERE invited_user_id = %s", (invited_user_id,))
        if cursor.fetchone():
            processing_invitations.remove(invitation_key)
            return False
        
        # 检查邀请人是否存在
        cursor.execute("SELECT id FROM user WHERE id = %s", (referrer_id,))
        if not cursor.fetchone():
            processing_invitations.remove(invitation_key)
            return False
        
        # 确保被邀请用户存在于user表中
        cursor.execute("SELECT id FROM user WHERE id = %s", (invited_user_id,))
        if not cursor.fetchone():
            # 如果用户不存在，则创建用户记录
            cursor.execute(
                "INSERT INTO user (id, name, coins) VALUES (%s, %s, %s)",
                (invited_user_id, invited_user_name, INVITATION_REWARD)
            )
        else:
            # 给被邀请用户增加金币奖励
            cursor.execute(
                "UPDATE user SET coins = coins + %s WHERE id = %s",
                (INVITATION_REWARD, invited_user_id)
            )
        
        # 添加邀请记录
        cursor.execute(
            "INSERT INTO user_invitations (invited_user_id, referrer_id, invitation_time, reward_claimed) VALUES (%s, %s, NOW(), TRUE)",
            (invited_user_id, referrer_id)
        )
        
        # 给邀请人增加金币奖励
        cursor.execute(
            "UPDATE user SET coins = coins + %s WHERE id = %s",
            (INVITATION_REWARD, referrer_id)
        )
        
        connection.commit()
        processing_invitations.remove(invitation_key)
        return True
    except Error as e:
        logger.error(f"Database error in add_invitation_record: {e}")
        if connection:
            try:
                connection.rollback()  # 回滚事务以确保数据一致性
            except Exception as rollback_error:
                logger.error(f"Error rolling back transaction: {rollback_error}")
        if invitation_key in processing_invitations:
            processing_invitations.remove(invitation_key)
        return False
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

async def async_add_invitation_record(invited_user_id, referrer_id, invited_user_name):
    """异步添加邀请记录"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        ref_executor, add_invitation_record, invited_user_id, referrer_id, invited_user_name
    )

def get_invited_users(user_id):
    """获取用户邀请的所有用户信息"""
    connection = None
    cursor = None
    try:
        connection = mysql_connection.create_connection()
        if not connection:
            logger.error("Failed to create database connection in get_invited_users")
            return 0, []
            
        cursor = connection.cursor()
        
        # 获取邀请总数
        cursor.execute(
            "SELECT COUNT(*) FROM user_invitations WHERE referrer_id = %s",
            (user_id,)
        )
        count = cursor.fetchone()[0]
        
        # 获取被邀请用户的详细信息
        cursor.execute(
            "SELECT i.invited_user_id, u.name, i.invitation_time "
            "FROM user_invitations i "
            "JOIN user u ON i.invited_user_id = u.id "
            "WHERE i.referrer_id = %s "
            "ORDER BY i.invitation_time DESC",
            (user_id,)
        )
        invited_users = cursor.fetchall()
        
        return count, invited_users
    except Error as e:
        logger.error(f"Database error in get_invited_users: {e}")
        return 0, []
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

async def async_get_invited_users(user_id):
    """异步获取用户邀请的所有用户信息"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        ref_executor, get_invited_users, user_id
    )

def get_user_name(user_id):
    """根据用户ID获取用户名"""
    connection = None
    cursor = None
    try:
        connection = mysql_connection.create_connection()
        if not connection:
            logger.error(f"Failed to create database connection in get_user_name for user_id: {user_id}")
            return None
            
        cursor = connection.cursor()
        
        # 获取用户名
        cursor.execute("SELECT name FROM user WHERE id = %s", (user_id,))
        result = cursor.fetchone()
        
        if result:
            return result[0]
        return None
    except Error as e:
        logger.error(f"Database error in get_user_name for user_id {user_id}: {e}")
        return None
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

async def async_get_user_name(user_id):
    """异步获取用户名的包装函数"""
    return await asyncio.get_event_loop().run_in_executor(ref_executor, get_user_name, user_id)

def get_referrer(user_id):
    """获取用户的邀请人信息"""
    connection = None
    cursor = None
    try:
        connection = mysql_connection.create_connection()
        if not connection:
            logger.error("Failed to create database connection in get_referrer")
            return None
            
        cursor = connection.cursor()
        
        # 查询用户的邀请人
        cursor.execute(
            "SELECT ui.referrer_id, u.name "
            "FROM user_invitations ui "
            "JOIN user u ON ui.referrer_id = u.id "
            "WHERE ui.invited_user_id = %s",
            (user_id,)
        )
        result = cursor.fetchone()
        
        return result  # 返回 (referrer_id, referrer_name) 或 None
    except Error as e:
        logger.error(f"Database error in get_referrer: {e}")
        return None
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

async def async_get_referrer(user_id):
    """异步获取用户的邀请人信息"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        ref_executor, get_referrer, user_id
    )

def check_user_exists(user_id):
    """检查用户是否存在于数据库中"""
    connection = None
    cursor = None
    try:
        connection = mysql_connection.create_connection()
        if not connection:
            logger.error(f"Failed to create database connection in check_user_exists for user_id: {user_id}")
            return False
            
        cursor = connection.cursor()
        
        cursor.execute("SELECT id FROM user WHERE id = %s", (user_id,))
        result = cursor.fetchone()
        
        return result is not None
    except Error as e:
        logger.error(f"Database error in check_user_exists for user_id {user_id}: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

async def async_check_user_exists(user_id):
    """异步检查用户是否存在的包装函数"""
    return await asyncio.get_event_loop().run_in_executor(ref_executor, check_user_exists, user_id)

# def record_group_addition(user_id, group_id, group_name, member_count):
#     """记录机器人被添加到群组的信息"""
#     query = """
#     INSERT INTO user_group_additions (user_id, group_id, group_name, member_count, addition_time)
#     VALUES (%s, %s, %s, %s, NOW())
#     ON DUPLICATE KEY UPDATE
#     group_name = VALUES(group_name),
#     member_count = VALUES(member_count),
#     addition_time = NOW()
#     """
#     try:
#         connection = mysql_connection.create_connection()
#         if connection is None:
#             return False
#             
#         cursor = connection.cursor()
#         cursor.execute(query, (user_id, group_id, group_name, member_count))
#         
#         # 如果成员数量达到要求，增加用户金币
#         if member_count >= MIN_GROUP_MEMBERS:
#             # 检查是否已领取奖励
#             check_query = "SELECT rewarded FROM user_group_additions WHERE user_id = %s AND group_id = %s"
#             cursor.execute(check_query, (user_id, group_id))
#             result = cursor.fetchone()
#             
#             # 如果未领取奖励，则发放
#             if result and result[0] == 0:
#                 # 更新为已领取状态
#                 update_query = "UPDATE user_group_additions SET rewarded = 1 WHERE user_id = %s AND group_id = %s"
#                 cursor.execute(update_query, (user_id, group_id))
#                 
#                 # 增加用户金币
#                 process_user.add_user_coins(user_id, GROUP_REWARD, f"添加机器人到群组 {group_name}")
#         
#         connection.commit()
#         return True
#     except Error as e:
#         logger.error(f"Database error in record_group_addition: {e}")
#         return False
#     finally:
#         if connection and connection.is_connected():
#             cursor.close()
#             connection.close()

# async def async_record_group_addition(user_id, group_id, group_name, member_count):
#     loop = asyncio.get_event_loop()
#     return await loop.run_in_executor(
#         ref_executor, record_group_addition, user_id, group_id, group_name, member_count
#     )

def setup_ref_handlers(application):
    """设置推广系统的命令处理器"""
    # 只添加ref命令，移除myref命令
    application.add_handler(CommandHandler("ref", ref_command))
    
    # 保留回调处理器以备将来扩展
    application.add_handler(CallbackQueryHandler(ref_callback, pattern=r"^ref_"))
