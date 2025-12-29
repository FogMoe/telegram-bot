import asyncio
from datetime import datetime, timedelta
from core import mysql_connection, process_user
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CommandHandler, CallbackQueryHandler
from core.command_cooldown import cooldown

# 全局锁，确保同一时间只有一个质押操作执行
lock = asyncio.Lock()

def get_total_coins():
    """获取系统中所有用户的硬币总数"""
    connection = mysql_connection.create_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT SUM(coins) FROM user")
        result = cursor.fetchone()
        return result[0] if result and result[0] else 0
    finally:
        cursor.close()
        connection.close()

def get_total_staked():
    """获取系统中所有质押的硬币总数"""
    connection = mysql_connection.create_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT SUM(stake_amount) FROM user_stakes")
        result = cursor.fetchone()
        return result[0] if result and result[0] else 0
    finally:
        cursor.close()
        connection.close()

def calculate_reward_rate():
    """计算当前回报率 - 基于质押比例"""
    total_coins = get_total_coins()
    total_staked = get_total_staked()
    
    if total_staked == 0 or total_coins == 0:
        return 3.0  # 如果没有质押或系统中没有金币，回报率默认为最高值3%
    
    # 计算质押比例（0到1之间的值）- 确保转换为浮点数
    stake_ratio = min(1.0, float(total_staked) / (float(total_coins)+float(total_staked)))  # 确保不超过1
    
    # 线性插值计算回报率：质押比例0%对应3%回报率，质押比例100%对应0.5%回报率
    max_rate = 3.0
    min_rate = 0.5
    reward_rate = max_rate - stake_ratio * (max_rate - min_rate)
    
    return reward_rate

def get_user_stake(user_id):
    """获取用户的质押信息"""
    connection = mysql_connection.create_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT stake_amount, stake_time, last_reward_time FROM user_stakes WHERE user_id = %s", 
            (user_id,)
        )
        result = cursor.fetchone()
        if not result:
            return None
        return {
            'stake_amount': result[0],
            'stake_time': result[1],
            'last_reward_time': result[2]
        }
    finally:
        cursor.close()
        connection.close()

def calculate_available_reward(user_id):
    """计算用户可领取的奖励金额"""
    user_stake = get_user_stake(user_id)
    if not user_stake or user_stake['stake_amount'] <= 0:
        return 0
    
    reward_rate = calculate_reward_rate()
    
    # 确定上次领取奖励的时间
    last_reward_time = user_stake['last_reward_time']
    if not last_reward_time:
        last_reward_time = user_stake['stake_time']
    
    # 计算从上次领取到现在已经过去了多少个完整的24小时
    now = datetime.now()
    hours_passed = (now - last_reward_time).total_seconds() / 3600
    days_passed = int(hours_passed / 24)  # 整数天数
    
    # 计算回报 - 确保转换为浮点数
    daily_reward = int(float(user_stake['stake_amount']) * (reward_rate / 100))
    total_reward = daily_reward * days_passed
    
    return total_reward

@cooldown
async def stake_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /stake 命令"""
    user_id = update.effective_user.id
    
    # 检查用户是否已注册
    if not process_user.user_exists(user_id):
        await update.message.reply_text(
            "请先使用 /me 命令注册您的账户。\n"
            "Please register first using the /me command."
        )
        return
    
    # 如果没有参数，显示当前质押状态
    if not context.args:
        await show_stake_status(update, context)
        return
    
    # 尝试质押指定数量的硬币
    try:
        amount = int(context.args[0])
        if amount <= 0:
            raise ValueError("质押金额必须为正整数")
        
        await stake_coins(update, context, amount)
    except ValueError:
        await update.message.reply_text(
            "请输入有效的质押金额。格式: /stake <数量>\n"
            "Please enter a valid stake amount. Format: /stake <amount>"
        )

async def show_stake_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """显示用户的质押状态和按钮"""
    user_id = update.effective_user.id
    user_stake = get_user_stake(user_id)
    reward_rate = calculate_reward_rate()
    
    # 构建状态消息 - 回报率保留2位小数
    status_message = f"当前质押回报率: {reward_rate:.2f}%/天\n"
    
    if user_stake:
        available_reward = calculate_available_reward(user_id)
        stake_time_str = user_stake['stake_time'].strftime('%Y-%m-%d %H:%M:%S')
        
        status_message += (
            f"您当前已质押: {user_stake['stake_amount']} 金币\n"
            f"质押时间: {stake_time_str}\n"
            f"可领取回报: {available_reward} 金币"
        )
        
        # 添加按钮
        keyboard = [
            [InlineKeyboardButton("领取回报", callback_data=f"stake_collect_{user_id}")],
            [InlineKeyboardButton("取出本金", callback_data=f"stake_withdraw_{user_id}")]
        ]
    else:
        status_message += (
            "您当前没有质押任何金币。\n"
            "使用 /stake <数量> 命令来质押金币。"
        )
        
        # 没有质押，不需要按钮
        keyboard = []
    
    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    
    await update.message.reply_text(
        status_message,
        reply_markup=reply_markup
    )

async def stake_coins(update: Update, context: ContextTypes.DEFAULT_TYPE, amount: int):
    """质押指定数量的金币"""
    user_id = update.effective_user.id
    
    async with lock:
        # 检查用户是否有足够的金币
        user_coins = process_user.get_user_coins(user_id)
        
        if user_coins < amount:
            await update.message.reply_text(
                f"您没有足够的金币。当前余额: {user_coins} 金币。\n"
                f"You don't have enough coins. Current balance: {user_coins} coins."
            )
            return
        
        connection = mysql_connection.create_connection()
        cursor = connection.cursor()
        
        try:
            # 检查用户是否已有质押
            existing_stake = get_user_stake(user_id)
            
            if existing_stake:
                await update.message.reply_text(
                    "您已经有质押的金币。如果要增加质押金额，请先取出当前质押。\n"
                    "You already have staked coins. If you want to increase your stake, please withdraw your current stake first."
                )
                return
            
            # 扣除用户的金币
            cursor.execute(
                "UPDATE user SET coins = coins - %s WHERE id = %s",
                (amount, user_id)
            )
            
            # 记录质押
            now = datetime.now()
            cursor.execute(
                "INSERT INTO user_stakes (user_id, stake_amount, stake_time) VALUES (%s, %s, %s)",
                (user_id, amount, now)
            )
            
            connection.commit()
            
            # 保留2位小数的回报率
            await update.message.reply_text(
                f"成功质押 {amount} 金币！当前回报率为 {calculate_reward_rate():.2f}%/天。\n"
                f"每24小时可领取一次回报。\n"
                f"Successfully staked {amount} coins! Current reward rate is {calculate_reward_rate():.2f}% everyday.\n"
                f"You can collect rewards once every 24 hours."
            )
        except Exception as e:
            connection.rollback()
            await update.message.reply_text(
                f"质押过程中发生错误: {str(e)}\n"
                f"Error occurred during staking: {str(e)}"
            )
        finally:
            cursor.close()
            connection.close()

async def stake_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理质押相关的按钮回调"""
    query = update.callback_query
    data = query.data.split('_')
    action = data[1]
    target_user_id = int(data[2])
    user_id = update.effective_user.id
    
    # 检查是否是目标用户点击的按钮
    if user_id != target_user_id:
        await query.answer("这不是你的质押，你不能操作。", show_alert=True)
        return
    
    if action == "collect":
        await collect_reward(query, user_id)
    elif action == "withdraw":
        await withdraw_stake(query, user_id)

async def collect_reward(query, user_id):
    """领取质押回报"""
    async with lock:
        connection = mysql_connection.create_connection()
        cursor = connection.cursor()
        
        try:
            # 获取用户的质押信息
            user_stake = get_user_stake(user_id)
            if not user_stake:
                await query.answer("您没有质押任何金币。", show_alert=True)
                return
            
            # 计算可领取的回报
            reward = calculate_available_reward(user_id)
            
            if reward <= 0:
                await query.answer("没有可领取的回报。需要等待至少24小时。", show_alert=True)
                return
            
            # 更新用户的金币数量
            cursor.execute(
                "UPDATE user SET coins = coins + %s WHERE id = %s",
                (reward, user_id)
            )
            
            # 更新上次领取奖励的时间
            cursor.execute(
                "UPDATE user_stakes SET last_reward_time = %s WHERE user_id = %s",
                (datetime.now(), user_id)
            )
            
            connection.commit()
            
            # 更新消息内容 - 保留2位小数的回报率
            await query.edit_message_text(
                f"您已成功领取 {reward} 金币的回报！\n"
                f"当前质押金额: {user_stake['stake_amount']} 金币\n"
                f"当前回报率: {calculate_reward_rate():.2f}%/天",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("领取回报", callback_data=f"stake_collect_{user_id}")],
                    [InlineKeyboardButton("取出本金", callback_data=f"stake_withdraw_{user_id}")]
                ])
            )
            
            await query.answer(f"成功领取 {reward} 金币回报！", show_alert=True)
        except Exception as e:
            connection.rollback()
            await query.answer(f"领取回报时发生错误: {str(e)}", show_alert=True)
        finally:
            cursor.close()
            connection.close()

async def withdraw_stake(query, user_id):
    """取出质押的本金"""
    async with lock:
        connection = mysql_connection.create_connection()
        cursor = connection.cursor()
        
        try:
            # 获取用户的质押信息
            user_stake = get_user_stake(user_id)
            if not user_stake:
                await query.answer("您没有质押任何金币。", show_alert=True)
                return
            
            stake_amount = user_stake['stake_amount']
            
            # 检查是否可以领取回报
            reward = calculate_available_reward(user_id)
            
            # 返还用户本金（不管是否有回报）
            cursor.execute(
                "UPDATE user SET coins = coins + %s WHERE id = %s",
                (stake_amount, user_id)
            )
            
            # 如果有可用回报，也一并发放
            if reward > 0:
                cursor.execute(
                    "UPDATE user SET coins = coins + %s WHERE id = %s",
                    (reward, user_id)
                )
                msg = f"您已取出质押本金 {stake_amount} 金币，并获得回报 {reward} 金币！"
            else:
                msg = f"您已取出质押本金 {stake_amount} 金币。\n未满24小时，无法获得回报。"
            
            # 删除质押记录
            cursor.execute(
                "DELETE FROM user_stakes WHERE user_id = %s",
                (user_id,)
            )
            
            connection.commit()
            
            # 更新消息内容 - 保留2位小数的回报率
            await query.edit_message_text(
                f"{msg}\n\n"
                f"当前质押回报率: {calculate_reward_rate():.2f}%/天\n"
                f"您目前没有质押金币。\n"
                f"使用 /stake <数量> 命令来质押金币。"
            )
            
            await query.answer(msg, show_alert=True)
        except Exception as e:
            connection.rollback()
            await query.answer(f"取出本金时发生错误: {str(e)}", show_alert=True)
        finally:
            cursor.close()
            connection.close()

# 创建质押相关的处理器
def setup_stake_handlers(application):
    """为质押系统设置处理器"""
    application.add_handler(CommandHandler("stake", stake_command))
    application.add_handler(CallbackQueryHandler(stake_callback, pattern=r"^stake_"))
