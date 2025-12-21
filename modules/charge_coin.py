import logging
import asyncio
from datetime import datetime
from threading import RLock
import re
import uuid  # 添加uuid模块导入
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes
import mysql_connection
import process_user
from command_cooldown import cooldown
import config

# 创建一个锁字典，用于防止同一卡密被并发使用
code_locks = {}
code_lock_mutex = RLock()  # 控制对code_locks字典的访问

# UUID格式的正则表达式
UUID_PATTERN = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.IGNORECASE)

# 管理员ID，用于权限验证
ADMIN_USER_ID = config.ADMIN_USER_ID  # 管理员的Telegram UserID

def is_valid_uuid(code):
    """验证字符串是否为有效的UUID格式"""
    return bool(UUID_PATTERN.match(code))

async def verify_and_use_code(user_id: int, code: str) -> tuple:
    """
    验证卡密并使用，确保原子操作
    
    返回: (成功与否, 金币数量或错误消息)
    """
    # 验证UUID格式
    if not is_valid_uuid(code):
        return False, "卡密格式无效，请确保输入了正确的充值卡密"
    
    # 先获取锁，防止同一卡密被并发请求使用
    with code_lock_mutex:
        if code in code_locks:
            return False, "此卡密正在被其他用户处理，请稍后再试"
        code_locks[code] = True

    try:
        # 创建数据库连接
        connection = mysql_connection.create_connection()
        cursor = connection.cursor()
        
        try:
            # 开启事务
            connection.start_transaction()
            
            # 查询卡密状态
            cursor.execute(
                "SELECT id, code, amount, is_used, used_by, used_at FROM redemption_codes WHERE code = %s FOR UPDATE", 
                (code,)
            )
            result = cursor.fetchone()
            
            # 检查卡密是否存在
            if not result:
                connection.rollback()
                return False, "无效的充值卡密，此卡密不存在或已被删除"
            
            code_id, db_code, amount, is_used, used_by, used_at = result
            
            # 检查卡密是否已被使用
            if is_used:
                used_time = used_at.strftime("%Y-%m-%d %H:%M:%S") if used_at else "未知时间"
                if used_by == user_id:
                    used_msg = f"此卡密已被您在 {used_time} 使用过"
                else:
                    used_msg = f"此卡密已被其他用户在 {used_time} 使用"
                connection.rollback()
                return False, used_msg
            
            # 标记卡密为已使用状态
            current_time = datetime.now()
            cursor.execute(
                "UPDATE redemption_codes SET is_used = TRUE, used_by = %s, used_at = %s WHERE id = %s",
                (user_id, current_time, code_id)
            )
            
            # 为用户添加金币
            cursor.execute(
                "UPDATE user SET coins = coins + %s WHERE id = %s",
                (amount, user_id)
            )
            
            # 提交事务
            connection.commit()
            return True, amount
            
        except Exception as e:
            # 发生错误时回滚事务
            connection.rollback()
            logging.error(f"充值卡密处理错误: {str(e)}")
            return False, f"充值处理过程中出现错误，请联系管理员"
        
        finally:
            cursor.close()
            connection.close()
            
    finally:
        # 无论成功与否，都释放锁
        with code_lock_mutex:
            if code in code_locks:
                del code_locks[code]


@cooldown
async def charge_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理充值命令: /charge <卡密>"""
    user_id = update.effective_user.id
    user_name = update.effective_user.username or str(user_id)
    
    # 检查用户是否已注册
    if not await process_user.async_user_exists(user_id):
        await update.message.reply_text(
            "❌ 请先使用 /me 命令注册个人信息后再使用充值功能。\n"
            "Please register first using the /me command before charging."
        )
        return
    
    # 检查是否提供了卡密参数
    if not context.args or len(context.args) != 1:
        await update.message.reply_text(
            "⚠️ 请输入正确的充值卡密！\n"
            "使用方法: /charge <卡密码>\n\n"
            "🔹 卡密格式例如: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx\n\n"
            "Please enter a valid redemption code!\n"
            "Usage: /charge <code>"
        )
        return
    
    # 获取卡密
    redemption_code = context.args[0].strip()
    
    # UUID格式预检查，避免明显错误的格式直接提交数据库
    if not is_valid_uuid(redemption_code):
        await update.message.reply_text(
            "❌ 卡密格式不正确！\n"
            "🔹 正确的卡密格式应为: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx\n"
            "例如: 123e4567-e89b-12d3-a456-426614174000\n\n"
            "Invalid code format! The correct format should be:\n"
            "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
        )
        return
    
    # 记录充值尝试
    logging.info(f"用户 {user_name}(ID:{user_id}) 尝试使用卡密: {redemption_code}")
    
    # 发送处理中消息
    processing_msg = await update.message.reply_text(
        "⏳ 正在处理您的充值请求，请稍候...\n"
        "Processing your charge request, please wait..."
    )
    
    # 验证并使用卡密
    success, result = await verify_and_use_code(user_id, redemption_code)
    
    if success:
        # 充值成功，获取用户当前金币
        current_coins = await process_user.async_get_user_coins(user_id)
        previous_coins = current_coins - result
        
        # 记录成功充值日志
        logging.info(f"用户 {user_name}(ID:{user_id}) 成功充值 {result} 金币，当前余额: {current_coins}")
        
        # 充值成功消息
        await processing_msg.edit_text(
            f"✅ 充值成功！\n\n"
            f"🎟️ 卡密: {redemption_code}\n"
            f"💰 充值金额: +{result} 金币\n"
            f"💳 充值前余额: {previous_coins} 金币\n"
            f"💎 当前余额: {current_coins} 金币\n\n"
            f"感谢您的支持！\n\n"
            f"Charge successful!\n"
            f"Added: {result} coins\n"
            f"Current balance: {current_coins} coins\n"
            f"Thank you for your support!"
        )
    else:
        # 记录充值失败日志
        logging.warning(f"用户 {user_name}(ID:{user_id}) 充值失败: {result}")
        
        # 充值失败，显示错误消息
        await processing_msg.edit_text(
            f"❌ 充值失败\n"
            f"原因: {result}\n\n"
            f"如需帮助，请联系机器人管理员 @ScarletKc\n\n"
            f"Charge failed\n"
            f"Reason: {result}\n"
            f"For assistance, please contact the bot admin @ScarletKc"
        )


@cooldown
async def admin_create_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """管理员命令：创建充值卡密 /create_code <数量> <金币>"""
    user_id = update.effective_user.id
    
    # 验证管理员权限 - 使用ADMIN_USER_ID常量
    if user_id != ADMIN_USER_ID:
        await update.message.reply_text("❌ 您没有足够的权限执行此操作\n您不是管理员")
        return
    
    # 检查参数格式
    if not context.args or len(context.args) != 2:
        await update.message.reply_text(
            "⚠️ 使用方法: /create_code <生成数量> <每个卡密的金币数>\n"
            "例如: /create_code 5 100"
        )
        return
    
    try:
        count = int(context.args[0])
        amount = int(context.args[1])
        
        if count <= 0 or count > 20:
            await update.message.reply_text("⚠️ 生成数量必须在1-20之间")
            return
        
        if amount <= 0 or amount > 10000:
            await update.message.reply_text("⚠️ 金币数量必须在1-10000之间")
            return
        
    except ValueError:
        await update.message.reply_text("⚠️ 参数必须为整数数字")
        return
    
    # 生成卡密
    connection = mysql_connection.create_connection()
    cursor = connection.cursor()
    
    try:
        codes = []
        duplicate_count = 0
        max_retries = 3  # 最大重试次数
        
        # 开始生成卡密
        for _ in range(count):
            retry_count = 0
            while retry_count < max_retries:
                # 使用Python的uuid模块生成UUID
                unique_code = str(uuid.uuid4())
                
                # 检查卡密是否已存在
                cursor.execute("SELECT id FROM redemption_codes WHERE code = %s", (unique_code,))
                if not cursor.fetchone():
                    # 卡密不存在，可以插入
                    cursor.execute(
                        "INSERT INTO redemption_codes (code, amount) VALUES (%s, %s)",
                        (unique_code, amount)
                    )
                    codes.append(unique_code)
                    break  # 成功插入，跳出重试循环
                
                retry_count += 1
                
            if retry_count >= max_retries:
                duplicate_count += 1
                logging.warning(f"生成唯一卡密失败，重试次数达到上限: {max_retries}")
        
        connection.commit()
        
        if duplicate_count > 0:
            await update.message.reply_text(
                f"⚠️ 注意: 有 {duplicate_count} 个卡密因重复而未能生成。实际生成了 {len(codes)} 个卡密。"
            )
        
        if not codes:
            await update.message.reply_text("❌ 未能生成任何卡密，请稍后再试")
            return
            
        # 生成卡密列表文本
        codes_text = "\n\n".join([f"{i+1}. `{code}` - {amount}金币" for i, code in enumerate(codes)])
        
        await update.message.reply_text(
            f"✅ 成功生成 {len(codes)} 个充值卡密，每个价值 {amount} 金币：\n\n"
            f"{codes_text}\n\n"
            f"💡 提示：请保存这些卡密，它们只会显示一次！"
        )
        
        # 记录操作日志
        logging.info(f"管理员 {update.effective_user.username or user_id} 生成了 {len(codes)} 个价值 {amount} 金币的卡密")
        
    except Exception as e:
        connection.rollback()
        logging.error(f"生成卡密出错: {str(e)}")
        await update.message.reply_text(f"❌ 生成卡密时出错: {str(e)}")
    finally:
        cursor.close()
        connection.close()


def setup_charge_handlers(application):
    """设置充值系统的处理器"""
    application.add_handler(CommandHandler("charge", charge_command))
    application.add_handler(CommandHandler("create_code", admin_create_code))
