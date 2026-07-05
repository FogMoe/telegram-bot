from core import mysql_connection
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, CommandHandler, MessageHandler, filters, CallbackQueryHandler
import asyncio
import logging
import os
import re
import time
import threading
from collections import defaultdict
from core.command_cooldown import cooldown
from pathlib import Path
from core.config import BASE_DIR

SPAM_FILE_PATH = BASE_DIR / "resources" / "spam_words.txt"
# 垃圾信息过滤缓存 {group_id: enabled}
spam_filter_cache = {}
cache_lock = threading.Lock()  # 缓存操作锁
CACHE_TIMEOUT = 300  # 缓存过期时间：5分钟

# 垃圾词列表缓存
spam_words = set()
spam_patterns = []
last_spam_file_update = 0
SPAM_FILE_UPDATE_INTERVAL = 600  # 垃圾词文件检查更新间隔：10分钟

# 自定义垃圾词缓存 {group_id: {"keywords": [关键词列表], "patterns": [正则列表], "last_updated": timestamp}}
custom_spam_words_cache = {}
custom_cache_lock = threading.Lock()  # 自定义垃圾词缓存操作锁
custom_loading_groups = set()

# 速率限制器 {chat_id: {user_id: count}}
warning_rate_limiter = defaultdict(lambda: defaultdict(int))
rate_limit_lock = threading.Lock()
WARNING_RESET_INTERVAL = 3600  # 警告计数重置时间：1小时

# 添加全局防抖字典，记录用户最后点击时间
callback_cooldown = {}
callback_lock = threading.Lock()
CALLBACK_COOLDOWN_TIME = 3  # 按钮冷却时间（秒）

# URL检测正则表达式 - 匹配大多数常见的URL格式
URL_PATTERN = re.compile(r'https?://\S+|www\.\S+|t\.me/\S+|\S+\.\S*|\S+\.(com|org|net|io|co|ru|cn|me|app|xyz|gov|edu)\b', re.IGNORECASE)

# @mention检测正则表达式 - 匹配Telegram的@username格式
MENTION_PATTERN = re.compile(r'@[a-zA-Z0-9_]+')

# 统一的帮助文本，在多处复用
SPAM_CONTROL_HELP_TEXT = (
    "🛡️ <b>垃圾信息过滤功能使用说明</b> 🛡️\n\n"
    "<b>基本命令：</b>\n"
    "/spam - 开启/关闭垃圾信息过滤\n"
    "/spam help - 显示此帮助信息\n\n"
    "<b>过滤设置：</b>\n"
    "/spam links on - 开启链接过滤\n"
    "/spam links off - 关闭链接过滤\n"
    "/spam mentions on - 开启@提及过滤\n"
    "/spam mentions off - 关闭@提及过滤\n\n"
    "<b>自定义垃圾词：</b>\n"
    "/spam list - 列出自定义垃圾词\n"
    "/spam add &lt;关键词&gt; - 添加自定义垃圾词\n"
    "/spam add //&lt;正则表达式&gt; - 添加正则表达式匹配\n"
    "/spam del &lt;关键词&gt; - 删除自定义垃圾词\n\n"
    "<b>注意事项：</b>\n"
    "• 启用链接过滤后，所有包含链接的消息将被自动删除\n"
    "• 启用@提及过滤后，所有包含@用户名的消息将被自动删除\n"
    "• 每个群组最多可设置10个自定义垃圾词\n"
    "• 有自定义垃圾词时，全局垃圾词库将不生效\n"
    "• 管理员发送的消息不会被检测\n"
    "• 使用前请确保机器人有删除消息的权限"
)

# 简化版帮助文本（当HTML显示失败时使用）
SPAM_CONTROL_HELP_TEXT_PLAIN = (
    "垃圾信息过滤功能命令说明：\n\n"
    "基本命令：\n"
    "/spam - 开启/关闭垃圾信息过滤\n"
    "/spam help - 显示此帮助信息\n\n"
    "过滤设置：\n"
    "/spam links on/off - 开启/关闭链接过滤\n"
    "/spam mentions on/off - 开启/关闭@提及过滤\n\n"
    "自定义垃圾词：\n"
    "/spam list - 列出自定义垃圾词\n"
    "/spam add <词> - 添加垃圾词\n"
    "/spam del <词> - 删除垃圾词\n"
)

# 从数据库加载群组的垃圾信息过滤状态
async def load_spam_control_status(group_id):
    """从数据库加载群组的垃圾信息过滤状态"""
    try:
        # 假设数据库结构已经正确设置，直接查询所有字段
        result = await mysql_connection.fetch_one(
            "SELECT enabled, block_links, block_mentions FROM group_spam_control WHERE group_id = %s",
            (group_id,),
        )
        
        with cache_lock:
            if result:
                spam_filter_cache[group_id] = {
                    "enabled": result[0],
                    "block_links": result[1],
                    "block_mentions": result[2],
                    "last_updated": time.time()
                }
                return result[0], result[1], result[2]
            else:
                spam_filter_cache[group_id] = {
                    "enabled": False,
                    "block_links": False,
                    "block_mentions": False,
                    "last_updated": time.time()
                }
                return False, False, False
    except Exception as e:
        logging.error(f"加载垃圾信息过滤状态时出错: {e}")
        # 继续抛出异常，以便调用者处理
        raise

async def is_spam_control_enabled(group_id):
    """检查群组是否启用垃圾信息过滤"""
    now = time.time()
    
    with cache_lock:
        if group_id in spam_filter_cache:
            cache_data = spam_filter_cache[group_id]
            if now - cache_data["last_updated"] < CACHE_TIMEOUT:
                return cache_data["enabled"]
    
    # 缓存不存在或已过期，从数据库加载
    enabled, _, _ = await load_spam_control_status(group_id)
    return enabled

async def is_link_blocking_enabled(group_id):
    """检查群组是否启用链接过滤"""
    now = time.time()
    
    with cache_lock:
        if group_id in spam_filter_cache:
            cache_data = spam_filter_cache[group_id]
            if now - cache_data["last_updated"] < CACHE_TIMEOUT:
                return cache_data.get("block_links", False)
    
    # 缓存不存在或已过期，从数据库加载
    _, block_links, _ = await load_spam_control_status(group_id)
    return block_links

async def is_mention_blocking_enabled(group_id):
    """检查群组是否启用@mention过滤"""
    now = time.time()
    
    with cache_lock:
        if group_id in spam_filter_cache:
            cache_data = spam_filter_cache[group_id]
            if now - cache_data["last_updated"] < CACHE_TIMEOUT:
                return cache_data.get("block_mentions", False)
    
    # 缓存不存在或已过期，从数据库加载
    _, _, block_mentions = await load_spam_control_status(group_id)
    return block_mentions

def contains_url(text):
    """检查文本是否包含URL"""
    if not text:
        return False, None
    
    match = URL_PATTERN.search(text)
    if match:
        return True, match.group(0)
    return False, None

def contains_mention(text):
    """检查文本是否包含@mention"""
    if not text:
        return False, None
    
    match = MENTION_PATTERN.search(text)
    if match:
        return True, match.group(0)
    return False, None

def load_spam_words():
    """从文件加载垃圾词列表"""
    global spam_words, spam_patterns, last_spam_file_update
    
    # 检查文件是否存在
    if not os.path.exists(SPAM_FILE_PATH):
        logging.warning(f"垃圾词列表文件未找到: {SPAM_FILE_PATH}")
        SPAM_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(SPAM_FILE_PATH, 'w', encoding='utf-8') as f:
            f.write("# 垃圾词列表，一行一个词语\n")
            f.write("博彩\n发财\n")
            f.write("# 使用//开头的行表示正则表达式匹配模式\n")
            f.write("//\\d+\\s*[元块]\\s*[充值提现]\n")
        logging.info(f"已创建默认垃圾词列表文件: {SPAM_FILE_PATH}")
    
    # 检查文件是否需要更新
    file_mtime = os.path.getmtime(SPAM_FILE_PATH)
    if file_mtime <= last_spam_file_update:
        return  # 文件未更新，无需重新加载
        
    try:
        new_spam_words = set()
        new_patterns = []
        
        with open(SPAM_FILE_PATH, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                
                # 处理正则表达式模式
                if line.startswith('//'):
                    pattern = line[2:].strip()  # 修复拼写错误：trip -> strip
                    try:
                        compiled = re.compile(pattern, re.IGNORECASE)
                        new_patterns.append(compiled)
                    except re.error:
                        logging.error(f"无效的正则表达式: {pattern}")
                else:
                    new_spam_words.add(line.lower())
                    
        # 更新全局变量
        spam_words = new_spam_words
        spam_patterns = new_patterns
        last_spam_file_update = file_mtime
        logging.info(f"已加载 {len(spam_words)} 个垃圾词和 {len(spam_patterns)} 个正则表达式模式")
    except Exception as e:
        logging.error(f"加载垃圾词列表时出错: {e}")

async def load_custom_spam_keywords(group_id):
    """从数据库加载群组的自定义垃圾词"""
    try:
        results = await mysql_connection.fetch_all(
            "SELECT keyword, is_regex FROM group_spam_keywords WHERE group_id = %s",
            (group_id,),
        )
        
        keywords = []
        patterns = []
        
        for keyword, is_regex in results:
            if is_regex:
                try:
                    compiled = re.compile(keyword, re.IGNORECASE)
                    patterns.append(compiled)
                except re.error:
                    logging.error(f"无效的自定义正则表达式: {keyword}")
            else:
                keywords.append(keyword.lower())
        
        with custom_cache_lock:
            custom_spam_words_cache[group_id] = {
                "keywords": keywords,
                "patterns": patterns,
                "last_updated": time.time()
            }
        
        return keywords, patterns
    except Exception as e:
        logging.error(f"加载自定义垃圾词时出错: {e}")
        return [], []

async def get_custom_spam_keywords(group_id):
    """获取群组的自定义垃圾词，优先使用缓存，优化数据库访问"""
    now = time.time()
    
    # 首先检查缓存是否存在且未过期
    with custom_cache_lock:
        if group_id in custom_spam_words_cache:
            cache_data = custom_spam_words_cache[group_id]
            # 如果缓存未过期，直接返回
            if now - cache_data["last_updated"] < CACHE_TIMEOUT:
                return cache_data["keywords"], cache_data["patterns"]
    
    # 二次检查：如果此群组正在被另一个协程加载，等待一小段时间后再次检查缓存
    with custom_cache_lock:
        is_loading = group_id in custom_loading_groups

    if is_loading:
        await asyncio.sleep(0.1)
        with custom_cache_lock:
            if group_id in custom_spam_words_cache:
                cache_data = custom_spam_words_cache[group_id]
                if now - cache_data["last_updated"] < CACHE_TIMEOUT:
                    return cache_data["keywords"], cache_data["patterns"]
    
    # 标记该群组为"正在加载"状态
    with custom_cache_lock:
        custom_loading_groups.add(group_id)
    
    try:
        # 从数据库加载
        keywords, patterns = await load_custom_spam_keywords(group_id)
        return keywords, patterns
    finally:
        # 无论加载成功与否，都移除"正在加载"标记
        with custom_cache_lock:
            custom_loading_groups.discard(group_id)

async def has_custom_spam_keywords(group_id):
    """检查群组是否有自定义垃圾词"""
    keywords, patterns = await get_custom_spam_keywords(group_id)
    return len(keywords) > 0 or len(patterns) > 0

async def is_spam_message(message_text, group_id):
    """检查消息是否为垃圾信息，返回(是否垃圾信息, 触发的关键词)"""
    if not message_text:
        return False, None

    # 检查群组是否有自定义垃圾词，有则优先使用
    custom_keywords, custom_patterns = await get_custom_spam_keywords(group_id)
    
    # 如果有自定义垃圾词，就只用自定义的
    if custom_keywords or custom_patterns:
        # 检查自定义垃圾词
        text_lower = message_text.lower()
        for word in custom_keywords:
            if word in text_lower:
                return True, word
        
        # 检查自定义正则表达式
        for pattern in custom_patterns:
            match = pattern.search(message_text)
            if match:
                matched_text = match.group(0) if match.group(0) else pattern.pattern
                return True, matched_text
        
        return False, None
    
    # 无自定义垃圾词，使用全局垃圾词列表
    # 检查文件是否需要更新
    now = time.time()
    if now - last_spam_file_update > SPAM_FILE_UPDATE_INTERVAL:
        load_spam_words()
    
    # 转为小写进行匹配
    text_lower = message_text.lower()
    
    # 检查垃圾词
    for word in spam_words:
        if word in text_lower:
            return True, word
    
    # 检查正则表达式模式
    for pattern in spam_patterns:
        match = pattern.search(message_text)
        if match:
            # 尝试返回匹配到的实际文本，如果无法获取则返回模式
            matched_text = match.group(0) if match.group(0) else pattern.pattern
            return True, matched_text
            
    return False, None

def update_warning_count(chat_id, user_id):
    """更新用户警告次数，返回当前警告次数"""
    with rate_limit_lock:
        # 获取当前计数
        warning_rate_limiter[chat_id][user_id] += 1
        return warning_rate_limiter[chat_id][user_id]

@cooldown
async def toggle_spam_control(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /spam 命令"""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    # 仅在群组中有效
    if update.effective_chat.type not in ["group", "supergroup"]:
        await update.message.reply_text("此命令只能在群组中使用。")
        return

    # 检查用户是否为群组管理员
    try:
        chat_member = await context.bot.get_chat_member(chat_id, user_id)
        if chat_member.status not in ["administrator", "creator"]:
            await update.message.reply_text("只有群组管理员才能使用此命令。")
            return
        is_admin = True  # 标记用户为管理员
    except Exception as e:
        logging.error(f"检查用户权限时出错: {str(e)}")
        await update.message.reply_text("检查权限时出错，请稍后再试。")
        return

    # 解析子命令
    if context.args:
        sub_command = context.args[0].lower()
        
        if sub_command == "links":
            if len(context.args) < 2:
                await update.message.reply_text(
                    "设置链接过滤的正确格式是：\n"
                    "/spam links on - 开启链接过滤\n"
                    "/spam links off - 关闭链接过滤"
                )
                return
            
            option = context.args[1].lower()
            if option == "on":
                await toggle_link_blocking(update, chat_id, user_id, True)
                return
            elif option == "off":
                await toggle_link_blocking(update, chat_id, user_id, False)
                return
            else:
                await update.message.reply_text("参数错误。请使用 on 或 off。")
                return
        
        elif sub_command == "mentions":
            if len(context.args) < 2:
                await update.message.reply_text(
                    "设置@提及过滤的正确格式是：\n"
                    "/spam mentions on - 开启@提及过滤\n"
                    "/spam mentions off - 关闭@提及过滤"
                )
                return
            
            option = context.args[1].lower()
            if option == "on":
                await toggle_mention_blocking(update, chat_id, user_id, True)
                return
            elif option == "off":
                await toggle_mention_blocking(update, chat_id, user_id, False)
                return
            else:
                await update.message.reply_text("参数错误。请使用 on 或 off。")
                return
        
        elif sub_command == "add":
            if len(context.args) < 2:
                await update.message.reply_text(
                    "添加自定义垃圾词的正确格式是：\n"
                    "/spam add <垃圾词>\n\n"
                    "添加正则表达式格式：\n"
                    "/spam add //正则表达式\n\n"
                    "示例：\n"
                    "/spam add 博彩\n"
                    "/spam add //\\d+元.*充值"
                )
                return
            
            keyword = " ".join(context.args[1:])
            await add_custom_spam_keyword(update, chat_id, user_id, keyword)
            return
            
        elif sub_command == "del":
            if len(context.args) < 2:
                await update.message.reply_text("删除自定义垃圾词的正确格式是：\n/spam del <垃圾词>")
                return
                
            keyword = " ".join(context.args[1:])
            await del_custom_spam_keyword(update, chat_id, keyword)
            return
            
        elif sub_command == "list":
            await list_custom_spam_keywords(update, chat_id)
            return
            
        elif sub_command == "help":
            await show_spam_control_help(update)
            return
    
    # 如果没有子命令或子命令不是add/del/list，切换垃圾信息过滤状态
    # 检查机器人是否有必要的权限
    try:
        bot_member = await context.bot.get_chat_member(chat_id, context.bot.id)
        if not bot_member.can_delete_messages:
            await update.message.reply_text("机器人需要有删除消息的权限才能启用垃圾信息过滤功能。请先授予机器人管理员权限并允许其删除消息。")
            return
    except Exception as e:
        logging.error(f"检查机器人权限时出错: {str(e)}")
        await update.message.reply_text("检查机器人权限时出错，请稍后再试。")
        return
    
    # 获取当前状态
    current_status = await is_spam_control_enabled(chat_id)
    
    # 切换状态
    new_status = not current_status
    
    try:
        # 查询当前设置以保留其他配置
        result = await mysql_connection.fetch_one(
            "SELECT block_links, block_mentions FROM group_spam_control WHERE group_id = %s",
            (chat_id,),
        )
        block_links = False
        block_mentions = False
        
        if result:
            block_links = result[0]
            block_mentions = result[1]
        
        if new_status:
            await mysql_connection.execute(
                """INSERT INTO group_spam_control (group_id, enabled, block_links, block_mentions, enabled_by) 
                VALUES (%s, TRUE, %s, %s, %s)
                ON DUPLICATE KEY UPDATE enabled = TRUE, enabled_by = %s, updated_at = CURRENT_TIMESTAMP""",
                (chat_id, block_links, block_mentions, user_id, user_id),
            )
        else:
            await mysql_connection.execute(
                """INSERT INTO group_spam_control (group_id, enabled, block_links, block_mentions, enabled_by) 
                VALUES (%s, FALSE, %s, %s, %s)
                ON DUPLICATE KEY UPDATE enabled = FALSE, enabled_by = %s, updated_at = CURRENT_TIMESTAMP""",
                (chat_id, block_links, block_mentions, user_id, user_id),
            )
        
        # 更新缓存，保留所有设置
        with cache_lock:
            if chat_id in spam_filter_cache:
                spam_filter_cache[chat_id].update({
                    "enabled": new_status,
                    "last_updated": time.time()
                })
            else:
                spam_filter_cache[chat_id] = {
                    "enabled": new_status,
                    "block_links": block_links,
                    "block_mentions": block_mentions,
                    "last_updated": time.time()
                }
        
        if new_status:
            # 只对管理员显示"查看更多功能"按钮
            if is_admin:
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("查看更多功能 (管理员)", callback_data="spam_help")]
                ])
                await update.message.reply_text(
                    "垃圾信息过滤功能已 ***开启***。我将自动删除可能的垃圾消息并发出警告。", 
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=keyboard
                )
            else:
                await update.message.reply_text(
                    "垃圾信息过滤功能已 ***开启***。我将自动删除可能的垃圾消息并发出警告。", 
                    parse_mode=ParseMode.MARKDOWN
                )
        else:
            await update.message.reply_text("垃圾信息过滤功能已 ***关闭***。", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logging.error(f"更新垃圾信息过滤状态时出错: {e}")
        await update.message.reply_text(f"操作失败: {str(e)}")

async def toggle_link_blocking(update: Update, chat_id: int, user_id: int, enable: bool):
    """开启或关闭群组链接过滤功能"""
    # 检查垃圾过滤功能是否已启用，只有启用垃圾过滤功能时才能设置链接过滤
    is_enabled = await is_spam_control_enabled(chat_id)
    if not is_enabled:
        await update.message.reply_text("请先开启垃圾信息过滤功能（使用 /spam 命令），才能设置链接过滤功能。")
        return
    
    # 检查机器人是否有必要的权限
    try:
        bot_member = await update.get_bot().get_chat_member(chat_id, update.get_bot().id)
        if not bot_member.can_delete_messages:
            await update.message.reply_text("机器人需要有删除消息的权限才能使用链接过滤功能。")
            return
    except Exception as e:
        logging.error(f"检查机器人权限时出错: {str(e)}")
        await update.message.reply_text("检查机器人权限时出错，请稍后再试。")
        return
    
    try:
        # 直接更新链接过滤设置，假设表结构已经正确
        await mysql_connection.execute(
            """UPDATE group_spam_control SET block_links = %s, updated_at = CURRENT_TIMESTAMP
            WHERE group_id = %s""",
            (enable, chat_id),
        )
        
        # 更新缓存
        with cache_lock:
            if chat_id in spam_filter_cache:
                spam_filter_cache[chat_id]["block_links"] = enable
                spam_filter_cache[chat_id]["last_updated"] = time.time()
        
        status_text = "开启" if enable else "关闭"
        await update.message.reply_text(
            f"链接过滤功能已{status_text}。"
            f"{'所有链接消息将被视为垃圾信息处理。' if enable else ''}"
        )
    except Exception as e:
        logging.error(f"更新链接过滤状态时出错: {e}")
        await update.message.reply_text(f"操作失败: {str(e)}")

async def toggle_mention_blocking(update: Update, chat_id: int, user_id: int, enable: bool):
    """开启或关闭群组@mention过滤功能"""
    # 检查垃圾过滤功能是否已启用，只有启用垃圾过滤功能时才能设置@mention过滤
    is_enabled = await is_spam_control_enabled(chat_id)
    if not is_enabled:
        await update.message.reply_text("请先开启垃圾信息过滤功能（使用 /spam 命令），才能设置@mention过滤功能。")
        return
    
    # 检查机器人是否有必要的权限
    try:
        bot_member = await update.get_bot().get_chat_member(chat_id, update.get_bot().id)
        if not bot_member.can_delete_messages:
            await update.message.reply_text("机器人需要有删除消息的权限才能使用@mention过滤功能。")
            return
    except Exception as e:
        logging.error(f"检查机器人权限时出错: {str(e)}")
        await update.message.reply_text("检查机器人权限时出错，请稍后再试。")
        return
    
    try:
        # 直接更新@mention过滤设置，假设表结构已经正确
        await mysql_connection.execute(
            """UPDATE group_spam_control SET block_mentions = %s, updated_at = CURRENT_TIMESTAMP
            WHERE group_id = %s""",
            (enable, chat_id),
        )
        
        # 更新缓存
        with cache_lock:
            if chat_id in spam_filter_cache:
                spam_filter_cache[chat_id]["block_mentions"] = enable
                spam_filter_cache[chat_id]["last_updated"] = time.time()
        
        status_text = "开启" if enable else "关闭"
        await update.message.reply_text(
            f"@mention过滤功能已{status_text}。"
            f"{'所有包含@mention的消息将被自动删除。' if enable else ''}"
        )
    except Exception as e:
        logging.error(f"更新@mention过滤状态时出错: {e}")
        await update.message.reply_text(f"操作失败: {str(e)}")

async def add_custom_spam_keyword(update: Update, chat_id: int, user_id: int, keyword: str):
    """添加自定义垃圾词"""
    # 检查是否为正则表达式
    is_regex = False
    if keyword.startswith('//'):
        is_regex = True
        keyword = keyword[2:].strip()  # 去除前缀
        
        # 验证正则表达式是否有效
        try:
            re.compile(keyword)
        except re.error:
            await update.message.reply_text(f"无效的正则表达式: {keyword}")
            return
    
    # 检查关键词长度
    if len(keyword) > 255:
        await update.message.reply_text("垃圾词太长，请不要超过255个字符。")
        return
    
    try:
        # 检查是否已存在该关键词
        existing_keyword = await mysql_connection.fetch_one(
            "SELECT id FROM group_spam_keywords WHERE group_id = %s AND keyword = %s",
            (chat_id, keyword),
        )
        
        # 检查自定义垃圾词数量是否达到上限
        if not existing_keyword:
            count_row = await mysql_connection.fetch_one(
                "SELECT COUNT(*) FROM group_spam_keywords WHERE group_id = %s",
                (chat_id,),
            )
            count = count_row[0] if count_row else 0
            
            if count >= 10:
                await update.message.reply_text("每个群组最多只能设置10个自定义垃圾词，请先删除一些再添加。")
                return
        
        # 添加或更新自定义垃圾词
        await mysql_connection.execute(
            """INSERT INTO group_spam_keywords (group_id, keyword, is_regex, created_by) 
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE is_regex = VALUES(is_regex), created_by = VALUES(created_by)""",
            (chat_id, keyword, is_regex, user_id),
        )
        
        # 更新缓存
        await load_custom_spam_keywords(chat_id)
        
        if existing_keyword:
            await update.message.reply_text(f"已更新自定义垃圾词: '{keyword}'")
        else:
            await update.message.reply_text(f"已添加自定义垃圾词: '{keyword}'")
    except Exception as e:
        logging.error(f"添加自定义垃圾词时出错: {e}")
        await update.message.reply_text(f"添加自定义垃圾词时出错: {str(e)}")

async def del_custom_spam_keyword(update: Update, chat_id: int, keyword: str):
    """删除自定义垃圾词"""
    try:
        # 如果输入的是正则表达式格式，去掉前缀
        if keyword.startswith('//'):
            keyword = keyword[2:].strip()
            
        rowcount = await mysql_connection.execute(
            "DELETE FROM group_spam_keywords WHERE group_id = %s AND keyword = %s",
            (chat_id, keyword),
        )
        
        if rowcount > 0:
            # 更新缓存
            await load_custom_spam_keywords(chat_id)
            await update.message.reply_text(f"已删除自定义垃圾词: '{keyword}'")
        else:
            await update.message.reply_text(f"未找到自定义垃圾词: '{keyword}'")
    except Exception as e:
        logging.error(f"删除自定义垃圾词时出错: {e}")
        await update.message.reply_text(f"删除自定义垃圾词时出错: {str(e)}")

async def list_custom_spam_keywords(update: Update, chat_id: int):
    """列出群组的自定义垃圾词"""
    try:
        keywords = await mysql_connection.fetch_all(
            "SELECT keyword, is_regex FROM group_spam_keywords WHERE group_id = %s",
            (chat_id,),
        )
        
        if not keywords:
            await update.message.reply_text("当前群组没有设置任何自定义垃圾词。")
            return
            
        message = "当前群组的自定义垃圾词列表：\n\n"
        for idx, (keyword, is_regex) in enumerate(keywords, 1):
            if is_regex:
                message += f"{idx}. 正则: '//{keyword}'\n"
            else:
                message += f"{idx}. 关键词: '{keyword}'\n"
                
        message += "\n使用 /spam add <垃圾词> 添加垃圾词\n"
        message += "使用 /spam del <垃圾词> 删除垃圾词"
        
        await update.message.reply_text(message)
    except Exception as e:
        logging.error(f"获取自定义垃圾词列表时出错: {e}")
        await update.message.reply_text(f"获取自定义垃圾词列表时出错: {str(e)}")

async def show_spam_control_help(update: Update):
    """显示垃圾信息过滤功能的帮助信息"""
    try:
        await update.message.reply_text(SPAM_CONTROL_HELP_TEXT, parse_mode=ParseMode.HTML)
    except Exception as e:
        logging.error(f"发送帮助信息时出错: {str(e)}")
        # 尝试不使用解析模式发送
        await update.message.reply_text(SPAM_CONTROL_HELP_TEXT_PLAIN)

async def spam_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理垃圾过滤帮助按钮回调，并验证点击者是否为管理员"""
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = update.effective_chat.id
    
    # 防抖处理：检查是否在冷却期内
    current_time = time.time()
    with callback_lock:
        user_key = f"{user_id}:{chat_id}:spam_help"
        last_click_time = callback_cooldown.get(user_key, 0)
        
        # 如果上次点击时间距现在小于冷却时间，则忽略此次点击
        if current_time - last_click_time < CALLBACK_COOLDOWN_TIME:
            await query.answer("请不要频繁点击按钮", show_alert=True)
            return
            
        # 记录本次点击时间
        callback_cooldown[user_key] = current_time
        
        # 清理过期的冷却记录（可选，提高内存效率）：
        for key in list(callback_cooldown.keys()):
            if current_time - callback_cooldown[key] > CALLBACK_COOLDOWN_TIME * 2:
                callback_cooldown.pop(key, None)
    
    # 验证点击者是否为管理员
    try:
        chat_member = await context.bot.get_chat_member(chat_id, user_id)
        if chat_member.status not in ["administrator", "creator"]:
            await query.answer("只有管理员可以查看此功能", show_alert=True)
            return
    except Exception as e:
        logging.error(f"验证用户权限时出错: {str(e)}")
        await query.answer("验证权限时出错，请稍后再试", show_alert=True)
        return
    
    # 显示"正在处理"状态
    await query.answer("正在加载帮助信息...")
    
    try:
        await query.edit_message_text(text=SPAM_CONTROL_HELP_TEXT, parse_mode=ParseMode.HTML)
    except Exception as e:
        logging.error(f"编辑消息时出错: {str(e)}")
        # 如果编辑失败，尝试发送新消息
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=SPAM_CONTROL_HELP_TEXT,
                parse_mode=ParseMode.HTML
            )
        except Exception as send_error:
            logging.error(f"发送帮助消息时出错: {str(send_error)}")
            # 最后尝试不使用解析模式发送
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=SPAM_CONTROL_HELP_TEXT_PLAIN
            )

# 添加帮助函数获取有效消息
def get_effective_message(update: Update):
    """获取有效的消息对象，无论是普通消息还是编辑后的消息"""
    return update.message or update.edited_message

async def process_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理消息并检查是否为垃圾信息"""
    # 获取有效消息
    effective_message = get_effective_message(update)
    
    # 提前检查消息是否为空或是否为文本消息
    if not effective_message or not effective_message.text:
        return
        
    # 仅在群组中处理消息
    if update.effective_chat.type not in ["group", "supergroup"]:
        return

    chat_id = update.effective_chat.id
    message_text = effective_message.text
    
    # 性能优化：对于很短的消息可以跳过复杂的检测
    if len(message_text) < 2:
        return
    
    # 提前检查群组是否启用了垃圾信息过滤（性能优化）
    if not await is_spam_control_enabled(chat_id):
        return
    
    user_id = effective_message.from_user.id
    
    # 添加缓存检查，减少管理员权限检查次数
    is_admin_cache_key = f"is_admin:{chat_id}:{user_id}"
    is_admin = context.chat_data.get(is_admin_cache_key, None)
    
    if is_admin is None:
        try:
            chat_member = await context.bot.get_chat_member(chat_id, user_id)
            is_admin = chat_member.status in ["administrator", "creator"]
            # 缓存结果5分钟
            context.chat_data[is_admin_cache_key] = is_admin
            context.chat_data[f"{is_admin_cache_key}_expire"] = time.time() + 300
        except Exception as e:
            logging.error(f"获取用户权限时出错: {e}")
            is_admin = False  # 如果出错，假设不是管理员（安全第一）
    # 检查缓存是否过期
    elif time.time() > context.chat_data.get(f"{is_admin_cache_key}_expire", 0):
        try:
            chat_member = await context.bot.get_chat_member(chat_id, user_id)
            is_admin = chat_member.status in ["administrator", "creator"]
            # 更新缓存
            context.chat_data[is_admin_cache_key] = is_admin
            context.chat_data[f"{is_admin_cache_key}_expire"] = time.time() + 300
        except Exception as e:
            logging.error(f"刷新用户权限缓存时出错: {e}")
            # 保留旧的缓存值
    
    if is_admin:
        return  # 跳过对管理员消息的检测
    
    # 首先检查链接过滤设置
    if await is_link_blocking_enabled(chat_id):
        has_url, found_url = contains_url(message_text)
        if has_url:
            user_mention = effective_message.from_user.mention_html()
            warning_count = update_warning_count(chat_id, user_id)
            
            try:
                # 删除包含链接的消息
                await context.bot.delete_message(
                    chat_id=chat_id,
                    message_id=effective_message.message_id
                )
                
                # 发送警告，使用隐藏文字格式
                warning_message = (
                    f"⚠️ 注意: {user_mention} 发送的消息包含链接 <tg-spoiler>{found_url}</tg-spoiler>，已被自动删除。\n"
                    f"本群组禁止发送链接。这是第 {warning_count} 次警告。"
                )
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=warning_message,
                    parse_mode='HTML'
                )
                
                # 记录日志
                logging.info(f"已删除链接消息 - 群组: {chat_id}, 用户: {user_id}, 链接: {found_url}, 内容: {message_text[:50]}...")
                
                return  # 已删除消息，不需要继续检查
                
            except Exception as e:
                logging.error(f"处理链接消息时出错: {e}")
    
    # 检查@mention过滤设置
    if await is_mention_blocking_enabled(chat_id):
        has_mention, found_mention = contains_mention(message_text)
        if has_mention:
            user_mention = effective_message.from_user.mention_html()
            warning_count = update_warning_count(chat_id, user_id)
            
            try:
                # 删除包含@mention的消息
                await context.bot.delete_message(
                    chat_id=chat_id,
                    message_id=effective_message.message_id
                )
                
                # 发送警告，使用隐藏文字格式
                warning_message = (
                    f"⚠️ 注意: {user_mention} 发送的消息包含@提及 <tg-spoiler>{found_mention}</tg-spoiler>，已被自动删除。\n"
                    f"本群组禁止@提及用户。这是第 {warning_count} 次警告。"
                )
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=warning_message,
                    parse_mode='HTML'
                )
                
                # 记录日志
                logging.info(f"已删除@mention消息 - 群组: {chat_id}, 用户: {user_id}, 提及: {found_mention}, 内容: {message_text[:50]}...")
                
                return  # 已删除消息，不需要继续检查
                
            except Exception as e:
                logging.error(f"处理@mention消息时出错: {e}")
    
    # 继续检查是否为垃圾信息
    is_spam, trigger_word = await is_spam_message(message_text, chat_id)
    if is_spam:
        user_mention = effective_message.from_user.mention_html()
        warning_count = update_warning_count(chat_id, user_id)
        
        try:
            # 删除垃圾消息
            await context.bot.delete_message(
                chat_id=chat_id,
                message_id=effective_message.message_id
            )
            
            # 发送警告，包含触发的关键词（使用隐藏文字格式）
            warning_message = (
                f"⚠️ 注意: {user_mention} 发送的消息包含垃圾内容 <tg-spoiler>{trigger_word}</tg-spoiler>，已被自动删除。\n"
                f"这是第 {warning_count} 次警告。持续发送垃圾信息可能导致被禁言或移出群组。"
            )
            await context.bot.send_message(
                chat_id=chat_id,
                text=warning_message,
                parse_mode='HTML'
            )
            
            # 记录日志
            logging.info(f"已删除垃圾消息 - 群组: {chat_id}, 用户: {user_id}, 触发词: {trigger_word}, 内容: {message_text[:50]}...")
            
        except Exception as e:
            logging.error(f"处理垃圾消息时出错: {e}")

def setup_spam_control_handlers(dispatcher):
    """注册垃圾信息过滤处理器，不再尝试创建数据库表"""
    # 初始化垃圾词列表
    load_spam_words()
    
    # 添加命令处理器
    dispatcher.add_handler(CommandHandler("spam", toggle_spam_control))
    
    # 添加回调查询处理器
    dispatcher.add_handler(CallbackQueryHandler(spam_help_callback, pattern=r"^spam_help$"))
    
    # 添加消息处理器，优先级较高以便在其他处理前先过滤垃圾信息
    # 修改过滤器以包含编辑后的消息
    dispatcher.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS & 
            (filters.UpdateType.MESSAGE | filters.UpdateType.EDITED_MESSAGE),
            process_message
        ),
        group=5  # 优先级高于关键词处理
    )
    
    # 定期清理警告计数器
    def reset_warning_counters():
        with rate_limit_lock:
            warning_rate_limiter.clear()
            logging.info("已清理垃圾信息警告计数器")
        # 递归设置下一次清理任务
        from threading import Timer
        timer = Timer(WARNING_RESET_INTERVAL, reset_warning_counters)
        timer.daemon = True
        timer.start()
    
    # 设置定时任务清理警告计数
    from threading import Timer
    timer = Timer(WARNING_RESET_INTERVAL, reset_warning_counters)
    timer.daemon = True
    timer.start()
