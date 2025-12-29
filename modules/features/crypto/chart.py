import logging
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler
from telegram.constants import ParseMode
from core import mysql_connection
import mysql.connector
from concurrent.futures import ThreadPoolExecutor
import asyncio
import time
from core.command_cooldown import cooldown  # 导入命令冷却装饰器

# 创建日志记录器
logger = logging.getLogger(__name__)

# 创建线程池执行器用于异步数据库操作
db_executor = ThreadPoolExecutor(max_workers=5)

# 创建缓存
token_cache = {}  # 群组ID -> (chain, ca) 的映射
cache_timestamps = {}  # 群组ID -> 缓存时间戳的映射
CACHE_EXPIRY = 600  # 缓存过期时间（秒），10分钟

# 创建数据库连接
def create_connection():
    return mysql_connection.create_connection()

# 为群组绑定代币
def bind_token_for_group(group_id, chain, ca, set_by):
    connection = create_connection()
    cursor = connection.cursor()
    try:
        # 使用参数化查询，检查是否已存在记录
        cursor.execute("SELECT * FROM group_chart_tokens WHERE group_id = %s", (group_id,))
        result = cursor.fetchone()
        
        if result:
            # 更新现有记录
            cursor.execute("UPDATE group_chart_tokens SET chain = %s, ca = %s, set_by = %s WHERE group_id = %s",
                          (chain, ca, set_by, group_id))
        else:
            # 插入新记录
            cursor.execute("INSERT INTO group_chart_tokens (group_id, chain, ca, set_by) VALUES (%s, %s, %s, %s)",
                          (group_id, chain, ca, set_by, ))
        
        connection.commit()
        
        # 更新缓存
        token_cache[group_id] = (chain, ca)
        cache_timestamps[group_id] = time.time()
        
        return True
    except mysql.connector.Error as e:
        logger.error(f"数据库错误: {str(e)}")
        return False
    finally:
        cursor.close()
        connection.close()

# 获取群组绑定的代币
def get_group_token(group_id):
    # 检查缓存是否存在且未过期
    current_time = time.time()
    if group_id in token_cache and group_id in cache_timestamps:
        if current_time - cache_timestamps[group_id] < CACHE_EXPIRY:
            logger.info(f"从缓存获取群组 {group_id} 的代币信息")
            return token_cache[group_id]
    
    # 缓存不存在或已过期，从数据库获取
    connection = create_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT chain, ca FROM group_chart_tokens WHERE group_id = %s", (group_id,))
        result = cursor.fetchone()
        
        # 更新缓存
        if result:
            token_cache[group_id] = result
            cache_timestamps[group_id] = current_time
            
        return result  # 返回(chain, ca)元组或None
    except mysql.connector.Error as e:
        logger.error(f"数据库错误: {str(e)}")
        return None
    finally:
        cursor.close()
        connection.close()

# 清理过期缓存
def clean_expired_cache():
    current_time = time.time()
    expired_keys = [
        group_id for group_id in cache_timestamps 
        if current_time - cache_timestamps[group_id] >= CACHE_EXPIRY
    ]
    
    for group_id in expired_keys:
        if group_id in token_cache:
            del token_cache[group_id]
        if group_id in cache_timestamps:
            del cache_timestamps[group_id]
    
    if expired_keys:
        logger.info(f"已清理 {len(expired_keys)} 个过期缓存条目")

# 异步包装函数
async def async_bind_token_for_group(group_id, chain, ca, set_by):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        db_executor,
        lambda: bind_token_for_group(group_id, chain, ca, set_by)
    )

async def async_get_group_token(group_id):
    # 定期清理过期缓存
    if len(cache_timestamps) > 0:
        clean_expired_cache()
        
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        db_executor,
        lambda: get_group_token(group_id)
    )

# 检查用户是否为群组管理员
async def is_user_admin(update: Update):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    try:
        # 获取用户在群组中的状态
        chat_member = await update.effective_chat.get_member(user_id)
        return chat_member.status in ['creator', 'administrator']
    except Exception as e:
        logger.error(f"检查管理员权限时出错: {str(e)}")
        return False

# 为群组删除代币绑定
def delete_token_for_group(group_id):
    connection = create_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("DELETE FROM group_chart_tokens WHERE group_id = %s", (group_id,))
        connection.commit()
        
        # 删除缓存
        if group_id in token_cache:
            del token_cache[group_id]
        if group_id in cache_timestamps:
            del cache_timestamps[group_id]
        
        return True
    except mysql.connector.Error as e:
        logger.error(f"数据库错误: {str(e)}")
        return False
    finally:
        cursor.close()
        connection.close()

# 异步包装函数
async def async_delete_token_for_group(group_id):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        db_executor,
        lambda: delete_token_for_group(group_id)
    )

# 命令处理函数
@cooldown
async def chart_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 确保在群组中使用
    if update.effective_chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("此命令只能在群组中使用。")
        return
    
    # 解析命令参数
    args = context.args
    
    # 如果是/chart bind命令
    if len(args) >= 3 and args[0].lower() == 'bind':
        # 检查用户是否为管理员
        if not await is_user_admin(update):
            await update.message.reply_text("只有群组管理员才能绑定代币。")
            return
        
        chain = args[1].lower()  # 获取链名称
        ca = args[2]  # 获取合约地址
        
        # 验证链名称
        valid_chains = ['sol', 'solana', 'eth', 'ethereum', 'blast', 'bsc', 'bnb']
        if chain not in valid_chains:
            await update.message.reply_text(f"不支持的链名称。支持的链: {', '.join(valid_chains)}")
            return
        
        # 标准化链名称
        if chain in ['sol', 'solana']:
            chain = 'sol'
        elif chain in ['eth', 'ethereum']:
            chain = 'eth'
        elif chain in ['bsc', 'bnb']:
            chain = 'bsc'
        
        # 绑定代币
        user_id = update.effective_user.id
        group_id = update.effective_chat.id
        
        success = await async_bind_token_for_group(group_id, chain, ca, user_id)
        
        if success:
            await update.message.reply_text(f"成功为群组绑定{chain}链上的代币。\n合约地址: {ca}")
        else:
            await update.message.reply_text("绑定代币失败，请稍后重试。")
        
        return
    
    # 如果是/chart clear命令（清除绑定）
    elif len(args) == 1 and args[0].lower() == 'clear':
        # 检查用户是否为管理员
        if not await is_user_admin(update):
            await update.message.reply_text("只有群组管理员才能清除代币绑定。")
            return
        
        group_id = update.effective_chat.id
        success = await async_delete_token_for_group(group_id)
        
        if success:
            await update.message.reply_text("成功清除了群组的代币绑定。")
        else:
            await update.message.reply_text("清除代币绑定失败，请稍后重试。")
        
        return
    
    # 如果是/chart命令（查看图表）
    elif len(args) == 0:
        group_id = update.effective_chat.id
        token_info = await async_get_group_token(group_id)
        
        if token_info:
            chain, ca = token_info
            chart_url = f"https://www.gmgn.cc/kline/{chain}/{ca}"
            
            # 添加链的显示名称映射
            chain_display_names = {
                'sol': 'Solana',
                'eth': 'Ethereum',
                'blast': 'Blast',
                'bsc': 'BSC'
            }
            chain_display = chain_display_names.get(chain, chain.upper())
            
            await update.message.reply_text(
                f"🔍 *代币图表*\n\n"
                f"链: {chain_display}\n"
                f"合约: `{ca}`\n\n"
                f"[点击查看图表]({chart_url})",
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=False
            )
        else:
            await update.message.reply_text(
                "此群组尚未绑定代币。\n\n"
                "管理员可使用以下命令绑定:\n"
                "/chart bind <chain> <CA>\n\n"
                "示例:\n"
                "/chart bind sol 2z9nPFtFRFwTTpQ6RpamUzsMfmF65Y3g14wu5FLj5rWC"
            )
        return
    
    # 如果命令格式不正确
    else:
        await update.message.reply_text(
            "命令格式不正确。\n\n"
            "查看当前群组绑定的代币图表:\n"
            "/chart\n\n"
            "管理员绑定代币:\n"
            "/chart bind <chain> <CA>\n\n"
            "管理员清除代币绑定:\n"
            "/chart clear\n\n"
            "示例:\n"
            "/chart bind sol 2z9nPFtFRFwTTpQ6RpamUzsMfmF65Y3g14wu5FLj5rWC"
        )

def setup_chart_handlers(application):
    """注册处理函数"""
    application.add_handler(CommandHandler("chart", chart_command))
    logger.info("已加载代币图表功能处理器")
