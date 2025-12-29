import logging
import hashlib
import re
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, CommandHandler

from core import mysql_connection
from core.command_cooldown import cooldown
import html

def hash_password(password):
    """对密码进行哈希处理"""
    return hashlib.sha256(password.encode('utf-8')).hexdigest()

def validate_password(password):
    """验证密码格式"""
    # 密码长度6-20位，包含字母和数字
    if len(password) < 6 or len(password) > 20:
        return False, "密码长度必须在6-20位之间"
    
    if not re.match(r'^[a-zA-Z0-9]+$', password):
        return False, "密码只能包含字母和数字"
    
    # 必须包含至少一个字母和一个数字
    if not re.search(r'[a-zA-Z]', password) or not re.search(r'[0-9]', password):
        return False, "密码必须包含至少一个字母和一个数字"
    
    return True, "密码格式正确"

async def get_user_web_password(user_id):
    """获取用户Web密码信息"""
    try:
        row = await mysql_connection.fetch_one(
            "SELECT password, created_at, updated_at FROM web_password WHERE user_id = %s",
            (user_id,),
            mapping=True,
        )
        return row  # RowMapping 或 None
    except Exception as e:
        logging.error(f"获取用户Web密码信息失败: {str(e)}")
        return None

async def set_user_web_password(user_id, password_hash):
    """设置用户Web密码"""
    try:
        # 使用 INSERT ... ON DUPLICATE KEY UPDATE 来更新或插入记录
        await mysql_connection.execute(
            """
            INSERT INTO web_password (user_id, password)
            VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE password = VALUES(password)
            """,
            (user_id, password_hash),
        )
        return True
    except Exception as e:
        logging.error(f"设置用户Web密码失败: {str(e)}")
        return False

async def process_set_web_password(user_id, password):
    """处理设置Web密码逻辑"""
    # 验证密码格式
    is_valid, message = validate_password(password)
    if not is_valid:
        return {
            "success": False,
            "message": message
        }
    
    # 对密码进行哈希处理
    password_hash = hash_password(password)
    
    # 检查是否已有密码
    existing_password = await get_user_web_password(user_id)
    is_update = existing_password is not None
    
    # 设置密码
    if await set_user_web_password(user_id, password_hash):
        action = "更新" if is_update else "设置"
        return {
            "success": True,
            "message": f"Web密码{action}成功！",
            "is_update": is_update
        }
    else:
        return {
            "success": False,
            "message": "设置Web密码时发生错误，请稍后再试"
        }

@cooldown
async def webpassword_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理/webpassword命令"""
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    
    # 检查用户名是否为空
    if not update.effective_user.username:
        await update.message.reply_text(
            "您需要设置Telegram用户名才能使用Web密码功能。\n"
            "请在Telegram设置中设置用户名后再尝试。\n\n"
            "You need to set a Telegram username to use the web password feature.\n"
            "Please set your username in Telegram settings and try again."
        )
        return
    
    # 转义用户名，防止HTML解析错误
    escaped_username = html.escape(username)
    
    # 检查用户是否注册
    if not await mysql_connection.async_check_user_exists(user_id):
        await update.message.reply_text(
            "请先使用 /me 命令注册账户。\n"
            "Please register first using the /me command."
        )
        return
    
    # 检查是否提供了密码参数
    if not context.args:
        # 显示当前密码状态
        password_info = await get_user_web_password(user_id)
        if password_info:
            message = (
                f"🔐 <b>Web密码状态</b>\n\n"
                f"用户: @{escaped_username}\n"
                f"状态: <b>已设置</b>\n"
                f"设置时间: {password_info['created_at'].strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"更新时间: {password_info['updated_at'].strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"使用方法: <code>/webpassword 新密码</code>\n"
                f"密码要求: 6-20位，包含字母和数字"
            )
        else:
            message = (
                f"🔐 <b>Web密码状态</b>\n\n"
                f"用户: @{escaped_username}\n"
                f"状态: <b>未设置</b>\n\n"
                f"使用方法: <code>/webpassword 新密码</code>\n"
                f"密码要求: 6-20位，包含字母和数字"
            )
        
        try:
            await update.message.reply_text(
                message,
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logging.error(f"Web密码状态消息HTML解析错误: {str(e)}")
            await update.message.reply_text(
                message.replace('<b>', '').replace('</b>', '').replace('<code>', '').replace('</code>', ''),
                parse_mode=None
            )
        return
    
    # 获取密码参数
    password = " ".join(context.args)
    
    # 异步处理设置密码
    result = await process_set_web_password(user_id, password)
    
    # 构建响应消息
    if result["success"]:
        action_text = "更新" if result["is_update"] else "设置"
        from datetime import datetime
        current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        message = (
            f"✅ <b>Web密码{action_text}成功</b>\n\n"
            f"用户: @{escaped_username}\n"
            f"操作: {action_text}Web密码\n"
            f"时间: {current_time}\n\n"
            f"⚠️ 请妥善保管您的密码，不要泄露给他人！"
        )
    else:
        message = (
            f"❌ <b>Web密码设置失败</b>\n\n"
            f"错误信息: {result['message']}\n\n"
            f"密码要求:\n"
            f"• 长度: 6-20位\n"
            f"• 字符: 仅限字母和数字\n"
            f"• 必须包含至少一个字母和一个数字"
        )
    
    # 发送消息
    try:
        await update.message.reply_text(
            message,
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        # 如果HTML解析失败，尝试不使用解析模式发送
        logging.error(f"Web密码消息HTML解析错误: {str(e)}")
        await update.message.reply_text(
            message.replace('<b>', '').replace('</b>', ''),  # 移除HTML标记
            parse_mode=None
        )

def setup_webpassword_handlers(application):
    """设置Web密码功能的处理器"""
    # 添加命令处理器
    application.add_handler(CommandHandler("webpassword", webpassword_command))
    
    # 日志记录
    logging.info("Web密码系统已初始化") 
