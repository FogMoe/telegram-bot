import time
import logging
import asyncio
import functools
from threading import RLock
from telegram import Update
from telegram.ext import ContextTypes

# 命令冷却时间（秒）
COOLDOWN_TIME = 1.0
# 聊天回复冷却时间（秒）
CHAT_COOLDOWN_TIME = 1.0

# 使用线程安全的字典存储用户的命令使用时间
# 格式: {user_id: {command_name: last_use_time}}
command_cooldowns = {}
# 新增: 聊天回复冷却时间字典
# 格式: {user_id: last_chat_time}
chat_cooldowns = {}
cooldown_lock = RLock()  # 使用可重入锁以确保线程安全

# 维护垃圾回收计时器
last_cleanup_time = time.time()
CLEANUP_INTERVAL = 3600  # 1小时清理一次过期数据

def cooldown(func):
    """
    命令冷却装饰器，为所有命令添加冷却时间
    
    用法:
    @cooldown
    async def some_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        ...
    """
    command_name = func.__name__
    
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        # 获取用户ID
        user_id = update.effective_user.id if update.effective_user else None
        
        # 没有用户ID的情况（如系统消息）直接执行
        if not user_id:
            return await func(update, context, *args, **kwargs)
        
        # 检查是否需要清理过期数据
        global last_cleanup_time
        current_time = time.time()
        if current_time - last_cleanup_time > CLEANUP_INTERVAL:
            cleanup_expired_cooldowns()
            last_cleanup_time = current_time
        
        # 检查用户是否在冷却期
        with cooldown_lock:
            user_cooldowns = command_cooldowns.get(user_id, {})
            last_used = user_cooldowns.get(command_name, 0)
            
            if current_time - last_used < COOLDOWN_TIME:
                # 用户在冷却期内
                remaining = COOLDOWN_TIME - (current_time - last_used)
                
                # 避免频繁回复 - 只有冷却时间超过0.5秒时才提示
                if remaining > 0.5:
                    try:
                        await update.message.reply_text(
                            f"请稍等片刻再使用此命令 ({remaining:.1f}秒)。\n"
                            f"Please wait a moment before using this command again ({remaining:.1f}s)."
                        )
                    except Exception as e:
                        # 如果回复失败（例如消息已删除），则静默忽略
                        logging.debug(f"无法发送冷却提示: {str(e)}")
                        
                return None  # 不执行命令
            
            # 更新用户的命令使用时间
            if user_id not in command_cooldowns:
                command_cooldowns[user_id] = {}
            command_cooldowns[user_id][command_name] = current_time
        
        # 执行原始命令
        return await func(update, context, *args, **kwargs)
    
    return wrapper

async def check_chat_cooldown(update: Update) -> bool:
    """
    检查用户聊天是否在冷却期内
    返回True表示可以继续，False表示在冷却期内
    """
    user_id = update.effective_user.id if update.effective_user else None
    
    # 没有用户ID的情况直接允许
    if not user_id:
        return True
        
    current_time = time.time()
    
    # 检查是否需要清理过期数据
    global last_cleanup_time
    if current_time - last_cleanup_time > CLEANUP_INTERVAL:
        cleanup_expired_cooldowns()
        last_cleanup_time = current_time
    
    # 检查用户是否在聊天冷却期
    with cooldown_lock:
        last_chat_time = chat_cooldowns.get(user_id, 0)
        
        if current_time - last_chat_time < CHAT_COOLDOWN_TIME:
            # 用户在冷却期内
            remaining = CHAT_COOLDOWN_TIME - (current_time - last_chat_time)
            
            # 避免频繁回复 - 只有冷却时间超过0.5秒时才提示
            if remaining > 0.5:
                try:
                    effective_message = update.message or update.edited_message
                    if effective_message:
                        await effective_message.reply_text(
                            f"请不要过于频繁地发送消息 ({remaining:.1f}秒)。\n"
                            f"Please don't send messages too frequently ({remaining:.1f}s)."
                        )
                except Exception as e:
                    # 如果回复失败，则静默忽略
                    logging.debug(f"无法发送聊天冷却提示: {str(e)}")
                    
            return False  # 在冷却期内
        
        # 更新用户的最后聊天时间
        chat_cooldowns[user_id] = current_time
        
        return True  # 允许聊天

def cleanup_expired_cooldowns():
    """清理过期的冷却数据以避免内存泄漏"""
    now = time.time()
    expired_threshold = now - 3600  # 1小时前的数据视为过期
    
    with cooldown_lock:
        # 清理命令冷却数据
        users_to_remove = []
        for user_id, commands in command_cooldowns.items():
            # 检查该用户的所有命令是否都已过期
            all_expired = True
            for cmd, last_time in list(commands.items()):
                if last_time > expired_threshold:
                    all_expired = False
                    break
            
            if all_expired:
                users_to_remove.append(user_id)
        
        # 删除过期用户命令数据
        for user_id in users_to_remove:
            del command_cooldowns[user_id]
        
        # 清理聊天冷却数据
        chat_users_to_remove = []
        for user_id, last_time in chat_cooldowns.items():
            if last_time < expired_threshold:
                chat_users_to_remove.append(user_id)
        
        # 删除过期用户聊天数据
        for user_id in chat_users_to_remove:
            del chat_cooldowns[user_id]
    
    logging.debug(
        f"冷却系统清理完成，移除了 {len(users_to_remove)} 个命令用户和 "
        f"{len(chat_users_to_remove)} 个聊天用户的过期数据"
    )
