import asyncio
import logging
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import RLock
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, CommandHandler, CallbackQueryHandler
import mysql_connection
from command_cooldown import cooldown
import html

# 创建线程池执行器用于异步数据库操作
checkin_executor = ThreadPoolExecutor(max_workers=5)
checkin_lock = RLock()  # 使用可重入锁以确保线程安全

def get_user_checkin_info(user_id):
    """获取用户签到信息"""
    connection = mysql_connection.create_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT last_checkin_date, consecutive_days FROM user_checkin WHERE user_id = %s", (user_id,))
        result = cursor.fetchone()
        return result  # (last_checkin_date, consecutive_days) 或 None
    except Exception as e:
        logging.error(f"获取用户签到信息失败: {str(e)}")
        return None
    finally:
        cursor.close()
        connection.close()

def update_user_checkin(user_id, consecutive_days):
    """更新用户签到信息"""
    today = datetime.now().date()
    connection = mysql_connection.create_connection()
    cursor = connection.cursor()
    try:
        # 使用 INSERT ... ON DUPLICATE KEY UPDATE 来更新或插入记录
        cursor.execute("""
        INSERT INTO user_checkin (user_id, last_checkin_date, consecutive_days)
        VALUES (%s, %s, %s)
        ON DUPLICATE KEY UPDATE last_checkin_date = VALUES(last_checkin_date), consecutive_days = VALUES(consecutive_days)
        """, (user_id, today, consecutive_days))
        connection.commit()
    except Exception as e:
        logging.error(f"更新用户签到信息失败: {str(e)}")
    finally:
        cursor.close()
        connection.close()

def calculate_checkin_reward(consecutive_days):
    """计算签到奖励金币数"""
    if consecutive_days >= 30:
        return 30  # 最高奖励上限30金币
    return min(consecutive_days, 30)  # 连续天数作为奖励，最高30金币

def process_checkin(user_id):
    """处理签到逻辑"""
    today = datetime.now().date()
    
    # 获取用户当前签到信息
    checkin_info = get_user_checkin_info(user_id)
    
    # 如果用户今天已经签到
    if checkin_info and checkin_info[0] == today:
        return {
            "success": False,
            "message": "您今天已经签到过了！请明天再来。",
            "consecutive_days": checkin_info[1]
        }
    
    # 计算连续签到天数
    consecutive_days = 1  # 默认为1天
    if checkin_info:
        last_checkin_date = checkin_info[0]
        # 如果昨天签到了，则连续天数+1
        if last_checkin_date == today - timedelta(days=1):
            consecutive_days = checkin_info[1] + 1
        # 如果之前有签到记录但不是昨天，则重置为1天
    
    # 计算奖励金币
    reward_coins = calculate_checkin_reward(consecutive_days)
    
    # 更新用户签到信息
    update_user_checkin(user_id, consecutive_days)
    
    # 更新用户金币
    import process_user
    process_user.update_user_coins(user_id, reward_coins)
    
    # 构建返回信息
    return {
        "success": True,
        "message": f"签到成功！\n连续签到：{consecutive_days}天\n获得奖励：{reward_coins}金币",
        "consecutive_days": consecutive_days,
        "reward": reward_coins
    }

async def async_process_checkin(user_id):
    """异步处理签到"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        checkin_executor,
        lambda: process_checkin(user_id)
    )

@cooldown
async def checkin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理/checkin命令"""
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    
    # 检查用户名是否为空
    if not update.effective_user.username:
        await update.message.reply_text(
            "您需要设置Telegram用户名才能使用签到功能。\n"
            "请在Telegram设置中设置用户名后再尝试。\n\n"
            "You need to set a Telegram username to use the check-in feature.\n"
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
    
    # 异步处理签到
    result = await async_process_checkin(user_id)
    
    # 构建响应消息
    if result["success"]:
        # 构建签到成功的消息
        message = (
            f"🎉 <b>签到成功</b> 🎉\n\n"
            f"用户: @{escaped_username}\n"
            f"连续签到: <b>{result['consecutive_days']}</b> 天\n"
            f"今日奖励: <b>{result['reward']}</b> 金币\n\n"
        )
        
        # 添加连续签到进度条
        days_left = min(30 - result['consecutive_days'], 29)
        if days_left > 0:
            message += f"距离最高奖励还有 {days_left} 天\n"
            progress = min(result['consecutive_days'], 30) / 30
            progress_bar = "".join(["🟢" if i/10 <= progress else "⚪" for i in range(1, 11)])
            message += f"{progress_bar} {int(progress*100)}%\n\n"
        else:
            message += "恭喜！你已达到最高奖励等级！🏆\n\n"
            
        message += "每天签到可获得金币奖励，连续签到奖励更多！"
        
    else:
        # 签到失败的消息
        message = (
            f"⚠️ {result['message']}\n\n"
            f"当前连续签到: <b>{result['consecutive_days']}</b> 天\n"
            f"请明天再来签到以继续你的连续签到记录！"
        )
    
    # 发送消息
    try:
        await update.message.reply_text(
            message,
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        # 如果HTML解析失败，尝试不使用解析模式发送
        logging.error(f"签到消息HTML解析错误: {str(e)}")
        await update.message.reply_text(
            message.replace('<b>', '').replace('</b>', ''),  # 移除HTML标记
            parse_mode=None
        )

def setup_checkin_handlers(application):
    """设置签到功能的处理器"""
    # 添加命令处理器
    application.add_handler(CommandHandler("checkin", checkin_command))
    
    # 日志记录
    logging.info("签到系统已初始化")
