import mysql_connection
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, CommandHandler, MessageHandler, filters
import logging
import time
import re
import threading
import html
from collections import defaultdict
from command_cooldown import cooldown

# HTML标签白名单
ALLOWED_HTML_TAGS = {
    'b', 'i', 'u', 'a', 'code', 'pre', 's', 'strong', 'em', 
    'ins', 'del', 'strike', 'span', 'tg-spoiler'
}

# 响应内容最大长度限制
MAX_RESPONSE_LENGTH = 1000

# 关键词缓存
# 格式: { group_id: {"keywords": [(keyword, response), ...], "last_updated": timestamp } }
keyword_cache = {}
cache_lock = threading.Lock()  # 用于保护缓存操作的锁
CACHE_TIMEOUT = 300  # 缓存过期时间：5分钟 = 300秒

# 速率限制器: {chat_id: {last_trigger_times: [时间戳列表]}}
rate_limiter = defaultdict(lambda: {"last_trigger_times": []})
rate_limit_lock = threading.Lock()  # 用于保护速率限制器的锁
MAX_TRIGGERS_PER_MINUTE = 5  # 每分钟最大触发次数


def sanitize_html(html_content):
    """净化HTML内容，仅允许白名单中的标签"""
    # 使用正则表达式匹配所有HTML标签
    tags = re.findall(r'</?([a-zA-Z0-9]+)[^>]*>', html_content)
    
    # 检查每个标签是否在白名单中
    for tag in set(tags):
        if tag.lower() not in ALLOWED_HTML_TAGS:
            # 如果不在白名单中，转义该标签
            html_content = re.sub(
                r'<(/?)' + re.escape(tag) + r'([^>]*)>', 
                r'&lt;\1' + tag + r'\2&gt;', 
                html_content
            )
    
    return html_content


def is_keyword_match(keyword, message):
    """使用更精确的关键词匹配方法"""
    # 单词边界匹配
    pattern = r'\b' + re.escape(keyword) + r'\b'
    if re.search(pattern, message, re.IGNORECASE):
        return True
    
    # 对于中文和其他没有明确单词边界的语言，
    # 可以直接做子字符串匹配，因为这些语言不使用空格分隔单词
    if not re.match(r'^[a-zA-Z0-9\s\W]+$', keyword):
        return keyword.lower() in message.lower()
    
    return False


def can_trigger_keyword(chat_id):
    """检查群组是否可以触发关键词（速率限制）"""
    with rate_limit_lock:
        now = time.time()
        group_data = rate_limiter[chat_id]
        trigger_times = group_data["last_trigger_times"]
        
        # 清理一分钟前的记录
        while trigger_times and now - trigger_times[0] > 60:
            trigger_times.pop(0)
        
        # 检查是否超过限制
        if len(trigger_times) >= MAX_TRIGGERS_PER_MINUTE:
            return False
        
        # 记录本次触发
        trigger_times.append(now)
        return True


# 从数据库加载指定群组的关键词
def load_keywords_from_db(chat_id):
    connection = mysql_connection.create_connection()
    cursor = connection.cursor()
    try:
        # 使用参数化查询，chat_id作为参数而非直接拼接
        cursor.execute(
            "SELECT keyword, response FROM group_keywords WHERE group_id = %s",
            (chat_id,)
        )
        keywords = cursor.fetchall()
        
        # 线程安全地更新缓存
        with cache_lock:
            keyword_cache[chat_id] = {
                "keywords": keywords,
                "last_updated": time.time()
            }
        return keywords
    except Exception as e:
        logging.error(f"从数据库加载关键词时出错: {str(e)}")
        return []
    finally:
        cursor.close()
        connection.close()


# 获取群组关键词（优先使用缓存）
def get_group_keywords(chat_id):
    now = time.time()
    
    # 线程安全地读取缓存
    with cache_lock:
        # 检查缓存是否存在且未过期
        if chat_id in keyword_cache:
            cache_data = keyword_cache[chat_id]
            # 如果缓存未过期，直接返回
            if now - cache_data["last_updated"] < CACHE_TIMEOUT:
                return cache_data["keywords"]
    
    # 缓存不存在或已过期，从数据库重新加载
    return load_keywords_from_db(chat_id)


@cooldown
async def keyword_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理/keyword命令，显示、添加或删除关键词"""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    # 仅在群组中有效
    if update.effective_chat.type not in ["group", "supergroup"]:
        await update.message.reply_text("此命令只能在群组中使用。\nThis command can only be used in groups.")
        return

    # 检查用户是否为群组管理员
    chat_member = await context.bot.get_chat_member(chat_id, user_id)
    if chat_member.status not in ["administrator", "creator"]:
        await update.message.reply_text("只有群组管理员才能使用此命令。\nOnly group administrators can use this command.")
        return

    # 解析子命令
    if not context.args:
        # 显示当前群组的关键词列表
        await show_keywords(update, chat_id)
        return

    sub_command = context.args[0].lower()
    
    if sub_command == "add":
        if len(context.args) < 3:
            await update.message.reply_text(
                "添加关键词的正确格式是：\n/keyword add <触发关键词> <回复内容>\n\n"
                "回复内容支持 Telegram HTML 格式。"
            )
            return
        
        keyword = context.args[1]
        response = " ".join(context.args[2:])
        await add_keyword(update, chat_id, user_id, keyword, response)
        
    elif sub_command == "del":
        if len(context.args) < 2:
            await update.message.reply_text("删除关键词的正确格式是：\n/keyword del <触发关键词>")
            return
            
        keyword = context.args[1]
        await del_keyword(update, chat_id, keyword)
        
    else:
        await update.message.reply_text(
            "未知的子命令。可用的命令有：\n"
            "/keyword - 显示关键词列表\n"
            "/keyword add <触发关键词> <回复内容> - 添加关键词\n"
            "/keyword del <触发关键词> - 删除关键词"
        )

async def show_keywords(update: Update, chat_id: int):
    """显示群组的关键词列表"""
    try:
        # 使用缓存机制获取关键词
        keywords = get_group_keywords(chat_id)
        
        if not keywords:
            await update.message.reply_text(
                "此群组尚未设置任何关键词。\n\n"
                "使用 /keyword add <触发关键词> <回复内容> 添加关键词\n"
                "使用 /keyword del <触发关键词> 删除关键词\n\n"
                "回复内容支持 Telegram HTML 格式。"
            )
            return
            
        message = "当前群组的关键词列表：\n\n"
        for idx, (keyword, response) in enumerate(keywords, 1):
            message += f"{idx}. 触发词: '{keyword}'\n"
            if len(response) > 30:
                message += f"   回复: '{response[:30]}...'\n\n"
            else:
                message += f"   回复: '{response}'\n\n"
        
        message += "\n使用 /keyword add <触发关键词> <回复内容> 添加关键词\n"
        message += "使用 /keyword del <触发关键词> 删除关键词"
        
        await update.message.reply_text(message)
    except Exception as e:
        logging.error(f"获取关键词列表时出错: {str(e)}")
        await update.message.reply_text(f"获取关键词列表时出错: {str(e)}")

async def add_keyword(update: Update, chat_id: int, user_id: int, keyword: str, response: str):
    """添加关键词"""
    if len(keyword) > 255:
        await update.message.reply_text("触发关键词太长，请不要超过255个字符。")
        return
        
    # 添加内容长度限制
    if len(response) > MAX_RESPONSE_LENGTH:
        await update.message.reply_text(f"回复内容太长，请不要超过{MAX_RESPONSE_LENGTH}个字符。")
        return
    
    # HTML净化
    response = sanitize_html(response)
    
    connection = mysql_connection.create_connection()
    cursor = connection.cursor()
    try:
        # 首先检查是否是更新现有关键词，安全使用参数
        cursor.execute("SELECT keyword FROM group_keywords WHERE group_id = %s AND keyword = %s", 
                      (chat_id, keyword))
        existing_keyword = cursor.fetchone()
        
        # 如果不是更新现有关键词，检查群组关键词数量是否已达上限
        if not existing_keyword:
            # 优先使用缓存判断关键词数量
            with cache_lock:
                if chat_id in keyword_cache and len(keyword_cache[chat_id]["keywords"]) >= 10:
                    await update.message.reply_text("每个群组最多只能设置10个关键词，请先删除一些关键词再添加。")
                    return
            
            # 缓存不存在或不确定，查询数据库
            cursor.execute("SELECT COUNT(*) FROM group_keywords WHERE group_id = %s", (chat_id,))
            keyword_count = cursor.fetchone()[0]
            
            if keyword_count >= 10:
                await update.message.reply_text("每个群组最多只能设置10个关键词，请先删除一些关键词再添加。")
                return
        
        # 添加或更新关键词，使用参数化查询防止注入
        cursor.execute(
            """INSERT INTO group_keywords (group_id, keyword, response, created_by) 
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE response = VALUES(response), created_by = VALUES(created_by)""",
            (chat_id, keyword, response, user_id)
        )
        connection.commit()
        
        # 更新成功后，强制刷新缓存
        load_keywords_from_db(chat_id)
        
        # 检查是更新还是新增
        if existing_keyword:
            await update.message.reply_text(f"已更新关键词触发器：'{keyword}'")
        else:
            await update.message.reply_text(f"已添加关键词触发器：'{keyword}'")
    except Exception as e:
        logging.error(f"添加关键词时出错: {str(e)}")
        await update.message.reply_text(f"添加关键词时出错: {str(e)}")
    finally:
        cursor.close()
        connection.close()

async def del_keyword(update: Update, chat_id: int, keyword: str):
    """删除关键词"""
    connection = mysql_connection.create_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "DELETE FROM group_keywords WHERE group_id = %s AND keyword = %s",
            (chat_id, keyword)
        )
        connection.commit()
        
        # 删除后，强制刷新缓存
        if cursor.rowcount > 0:
            load_keywords_from_db(chat_id)
            await update.message.reply_text(f"已删除关键词触发器：'{keyword}'")
        else:
            await update.message.reply_text(f"未找到关键词：'{keyword}'")
    except Exception as e:
        logging.error(f"删除关键词时出错: {str(e)}")
        await update.message.reply_text(f"删除关键词时出错: {str(e)}")
    finally:
        cursor.close()
        connection.close()

# 添加帮助函数获取有效消息
def get_effective_message(update: Update):
    """获取有效的消息对象，无论是普通消息还是编辑后的消息"""
    return update.message or update.edited_message

async def process_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理群组消息，检查是否触发关键词"""
    # 获取有效消息
    effective_message = get_effective_message(update)
    
    # 仅处理群组消息且不处理命令
    if (update.effective_chat.type not in ["group", "supergroup"] or 
        not effective_message or 
        not effective_message.text or 
        effective_message.text.startswith('/')):
        return
        
    chat_id = update.effective_chat.id
    message_text = effective_message.text
    
    # 使用缓存获取关键词
    keywords = get_group_keywords(chat_id)
    
    if not keywords:
        return
    
    # 检查速率限制
    if not can_trigger_keyword(chat_id):
        # 触发过于频繁，静默忽略
        return
        
    for keyword, response in keywords:
        # 使用更精确的匹配方法
        if is_keyword_match(keyword, message_text):
            try:
                # 发送前再次净化HTML
                safe_response = sanitize_html(response)
                await effective_message.reply_text(safe_response, parse_mode=ParseMode.HTML)
            except Exception as e:
                # 如果HTML解析失败，尝试不使用解析模式发送
                logging.warning(f"HTML解析失败，尝试纯文本: {str(e)}")
                await effective_message.reply_text(
                    f"【回复内容HTML格式错误】\n\n{html.escape(response)}"
                )
            break  # 只触发第一个匹配的关键词

def setup_keyword_handlers(dispatcher):
    """注册关键词相关的处理器"""
    
    # 添加命令处理器
    dispatcher.add_handler(CommandHandler("keyword", keyword_command))
    
    # 添加消息处理器，优先级设置较低以便其他功能先处理
    # 修改过滤器以包含编辑后的消息
    dispatcher.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS &
            (filters.UpdateType.MESSAGE | filters.UpdateType.EDITED_MESSAGE),
            process_group_message
        ),
        group=10  # 设置较低的优先级
    )
