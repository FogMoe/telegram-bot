import asyncio
import re
from datetime import datetime
import mysql_connection
import process_user
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler
from telegram.constants import ParseMode
from command_cooldown import cooldown

# 定义最低兑换数量
MIN_SWAP_AMOUNT = 10000

# 全局锁，确保兑换操作的原子性
lock = asyncio.Lock()

# 验证Solana钱包地址格式
def is_valid_solana_address(address):
    """检查字符串是否符合Solana钱包地址格式"""
    # Solana地址为44个字符的base58编码字符串
    solana_pattern = re.compile(r'^[1-9A-HJ-NP-Za-km-z]{43,44}$')
    return bool(solana_pattern.match(address))

def has_pending_swap_request(user_id):
    """检查用户是否有未完成的兑换请求"""
    connection = mysql_connection.create_connection()
    cursor = connection.cursor()
    try:
        query = "SELECT COUNT(*) FROM token_swap_requests WHERE user_id = %s AND status = 'pending'"
        cursor.execute(query, (user_id,))
        result = cursor.fetchone()
        return result[0] > 0 if result else False
    finally:
        cursor.close()
        connection.close()

def get_pending_swap_request(user_id):
    """获取用户未完成的兑换请求详情"""
    connection = mysql_connection.create_connection()
    cursor = connection.cursor()
    try:
        query = """
        SELECT amount, wallet_address, request_time 
        FROM token_swap_requests 
        WHERE user_id = %s AND status = 'pending'
        ORDER BY request_time DESC
        LIMIT 1
        """
        cursor.execute(query, (user_id,))
        result = cursor.fetchone()
        if result:
            return {
                "amount": result[0],
                "wallet_address": result[1],
                "request_time": result[2]
            }
        return None
    finally:
        cursor.close()
        connection.close()

@cooldown
async def swap_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理/swap命令，直接处理兑换流程"""
    user_id = update.effective_user.id
    username = update.effective_user.username or "Unknown"
    
    # 检查用户是否已注册
    if not await process_user.async_user_exists(user_id):
        await update.message.reply_text(
            "***请先使用 /me 命令注册您的账户。***\n"
            "Please register first using the /me command.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    # 检查用户是否有待处理的兑换请求
    if has_pending_swap_request(user_id):
        # 获取现有请求的详细信息
        pending_request = get_pending_swap_request(user_id)
        if pending_request:
            request_time_str = pending_request["request_time"].strftime("%Y-%m-%d %H:%M:%S")
            await update.message.reply_text(
                f"***您已有一个正在处理中的兑换请求***\n\n"
                f"金币数量: ***{pending_request['amount']}***\n"
                f"接收钱包: ***{pending_request['wallet_address']}***\n"
                f"申请时间: ***{request_time_str}***\n\n"
                f"请等待该请求处理完成后再进行新的兑换操作。处理时间可能长达72小时。\n\n"
                f"***You already have a pending exchange request***\n\n"
                f"Amount: ***{pending_request['amount']}*** coins\n"
                f"Receiving wallet: ***{pending_request['wallet_address']}***\n"
                f"Request time: ***{request_time_str}***\n\n"
                f"Please wait for it to be processed before making a new exchange. Processing may take up to 72 hours.",
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await update.message.reply_text(
                "***您已有一个正在处理中的兑换请求。***\n"
                "请等待该请求完成后再进行新的兑换操作。\n\n"
                "***You already have a pending exchange request.***\n"
                "Please wait for it to be processed before making a new exchange.",
                parse_mode=ParseMode.MARKDOWN
            )
        return
    
    # 如果没有参数或参数数量不正确，显示帮助信息
    if not context.args or len(context.args) != 2:
        await update.message.reply_text(
            "***该命令用于将您的金币兑换为Solana链上的$FOGMOE代币***\n\n"
            "用法: `/swap <金币数量> <Solana钱包地址>`\n"
            "示例: `/swap 10000 5iz3epFDf9SKvLNHWQ42f4wMMrENaudE9eMkxfBLFd2n`\n\n"
            "***最低兑换数量为10000金币。***\n"
            "***当前兑换比例为1:1（1金币=1$FOGMOE），该比例可能随时调整，最终兑换比例以实际操作为准。***\n\n"
            "注意: 兑换处理时间可能长达72小时。\n\n"
            "访问 [token.fog.moe](https://token.fog.moe/) 了解关于$FOGMOE代币的详细信息。\n\n"
            "***This command is used to exchange your coins for $FOGMOE tokens on the Solana chain***\n\n"
            "Usage: `/swap <amount> <Solana wallet address>`\n"
            "Example: `/swap 10000 5iz3epFDf9SKvLNHWQ42f4wMMrENaudE9eMkxfBLFd2n`\n\n"
            "***Minimum exchange amount is 10000 coins.***\n"
            "***Current exchange rate is 1:1 (1 coin = 1 $FOGMOE). This rate may change at any time, the final exchange rate will be determined at the time of processing.***\n\n"
            "Note: Processing time may take up to 72 hours.\n\n"
            "Visit [token.fog.moe](https://token.fog.moe/) to learn more about $FOGMOE tokens.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    # 尝试解析金币数量
    try:
        amount = int(context.args[0])
        if amount < MIN_SWAP_AMOUNT:
            await update.message.reply_text(
                f"***最低兑换数量为{MIN_SWAP_AMOUNT}金币。***\n"
                f"***Minimum exchange amount is {MIN_SWAP_AMOUNT} coins.***",
                parse_mode=ParseMode.MARKDOWN
            )
            return
    except ValueError:
        await update.message.reply_text(
            "***请输入有效的金币数量。***\n"
            "***Please enter a valid coin amount.***",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    # 验证钱包地址
    wallet_address = context.args[1].strip()
    if not is_valid_solana_address(wallet_address):
        await update.message.reply_text(
            "***您输入的不是有效的Solana钱包地址。***\n"
            "请确认后重试。\n\n"
            "***The address you entered is not a valid Solana wallet address.***\n"
            "Please check and try again.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    # 检查用户是否有足够的金币
    user_coins = await process_user.async_get_user_coins(user_id)
    if user_coins < amount:
        await update.message.reply_text(
            f"***您的金币不足。***\n"
            f"当前余额: ***{user_coins}*** 金币，需要: ***{amount}*** 金币。\n\n"
            f"***You don't have enough coins.***\n"
            f"Current balance: ***{user_coins}*** coins, required: ***{amount}*** coins.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    async with lock:
        # 再次检查是否有待处理的兑换请求
        if has_pending_swap_request(user_id):
            # 获取现有请求的详细信息
            pending_request = get_pending_swap_request(user_id)
            if pending_request:
                request_time_str = pending_request["request_time"].strftime("%Y-%m-%d %H:%M:%S")
                await update.message.reply_text(
                    f"***您已有一个正在处理中的兑换请求***\n\n"
                    f"金币数量: ***{pending_request['amount']}***\n"
                    f"接收钱包: ***{pending_request['wallet_address']}***\n"
                    f"申请时间: ***{request_time_str}***\n\n"
                    f"请等待该请求处理完成后再进行新的兑换操作。处理时间可能长达72小时。\n\n"
                    f"***You already have a pending exchange request***\n\n"
                    f"Amount: ***{pending_request['amount']}*** coins\n"
                    f"Receiving wallet: ***{pending_request['wallet_address']}***\n"
                    f"Request time: ***{request_time_str}***\n\n"
                    f"Please wait for it to be processed before making a new exchange. Processing may take up to 72 hours.",
                    parse_mode=ParseMode.MARKDOWN
                )
            return
        
        connection = mysql_connection.create_connection()
        cursor = connection.cursor()
        try:
            # 检查用户是否仍有足够金币（再次确认）
            select_query = "SELECT coins FROM user WHERE id = %s"
            cursor.execute(select_query, (user_id,))
            result = cursor.fetchone()
            if not result or result[0] < amount:
                await update.message.reply_text(
                    "***您的金币不足，无法完成兑换。***\n"
                    "***You don't have enough coins to complete this exchange.***",
                    parse_mode=ParseMode.MARKDOWN
                )
                return
            
            # 扣除用户金币
            update_query = "UPDATE user SET coins = coins - %s WHERE id = %s"
            cursor.execute(update_query, (amount, user_id))
            
            # 记录兑换请求
            insert_query = """
            INSERT INTO token_swap_requests (user_id, username, wallet_address, amount) 
            VALUES (%s, %s, %s, %s)
            """
            cursor.execute(insert_query, (user_id, username, wallet_address, amount))
            
            connection.commit()
            
            # 通知用户兑换请求已提交
            await update.message.reply_text(
                f"***您已成功提交兑换请求：***\n\n"
                f"金币数量: ***{amount}***\n"
                f"接收钱包: ***{wallet_address}***\n\n"
                f"***当前兑换比例为1:1（1金币=1$FOGMOE），该比例可能随时调整，最终兑换比例以实际处理为准。***\n\n"
                f"请耐心等待处理，兑换可能需要长达72小时。完成后，$FOGMOE代币将发送到您提供的钱包地址。\n\n"
                f"访问 [token.fog.moe](https://token.fog.moe/) 了解关于$FOGMOE代币的详细信息。\n\n"
                f"***You have successfully submitted an exchange request:***\n\n"
                f"Amount: ***{amount}*** coins\n"
                f"Receiving wallet: ***{wallet_address}***\n\n"
                f"***Current exchange rate is 1:1 (1 coin = 1 $FOGMOE). This rate may change at any time, the final exchange rate will be determined at the time of processing.***\n\n"
                f"Please be patient as processing may take up to 72 hours. Once completed, $FOGMOE tokens will be sent to the wallet address you provided.\n\n"
                f"Visit [token.fog.moe](https://token.fog.moe/) to learn more about $FOGMOE tokens.",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            connection.rollback()
            await update.message.reply_text(
                f"***兑换过程中出现错误:*** {str(e)}\n"
                f"请稍后重试。\n\n"
                f"***Error occurred during exchange:*** {str(e)}\n"
                f"Please try again later.",
                parse_mode=ParseMode.MARKDOWN
            )
        finally:
            cursor.close()
            connection.close()

def setup_swap_handler(application):
    """为代币兑换系统设置处理器"""
    # 使用普通的CommandHandler替代ConversationHandler
    application.add_handler(CommandHandler("swap", swap_command))
