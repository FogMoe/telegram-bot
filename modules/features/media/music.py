import logging
import asyncio
import aiohttp
import re
import time
import html
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CommandHandler, ContextTypes, CallbackQueryHandler
from core import process_user
from core.command_cooldown import cooldown
from collections import defaultdict

# 创建一个日志记录器
logger = logging.getLogger(__name__)

# API URL
MUSIC_API_URL = "https://api.jkyai.top/API/hqyyid.php"

# 请求头
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/96.0.4664.110 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "Cache-Control": "max-age=0"
}

# 帮助信息
HELP_TEXT = """
🎵 **音乐搜索使用说明** 🎵

基本命令:
• `/music <关键词>` - 搜索歌曲信息
• `/music help` - 显示此帮助信息

高级选项:
• 搜索后可以选择不同的音乐平台
• 支持网易云音乐、QQ音乐、酷我音乐、咪咕音乐、千千音乐
• 支持翻页查看更多结果

提示：
• 搜索结果显示歌曲名称、专辑、歌手等信息
• 如需精确搜索，可使用完整歌名，如：`/music again`
"""

# 音乐平台映射
PLATFORM_MAP = {
    "wy": "网易云音乐",
    "qq": "QQ音乐",
    "kw": "酷我音乐",
    "mg": "咪咕音乐",
    "qi": "千千音乐"
}

# 音乐链接格式映射
MUSIC_URL_FORMAT = {
    "wy": "https://music.163.com/#/song?id={}",   # 网易云音乐
    "qq": "https://y.qq.com/n/ryqq/songDetail/{}",  # QQ音乐
    "kw": "https://www.kuwo.cn/play_detail/{}",    # 酷我音乐
    "mg": "https://music.migu.cn/v3/music/song/{}", # 咪咕音乐
    "qi": "https://music.91q.com/player?songIds={}" # 千千音乐
}

# 正在处理的请求，格式: {user_id: {callback_data: timestamp}}
PROCESSING_REQUESTS = {}

# 查询结果缓存，格式: {cache_key: {"data": data, "timestamp": timestamp}}
RESULTS_CACHE = {}

# 用户全局请求速率限制
# 格式: {user_id: [timestamp1, timestamp2, ...]}
USER_RATE_LIMITS = defaultdict(list)

# 速率限制设置
RATE_LIMIT_WINDOW = 10  # 时间窗口（秒）
RATE_LIMIT_MAX_REQUESTS = 5  # 窗口内最大请求数
RATE_LIMIT_COOLDOWN = 15  # 超限后冷却时间（秒）

# 用户冷却状态 {user_id: cool_until_timestamp}
USER_COOLDOWNS = {}

# 处理超时时间（秒）
REQUEST_TIMEOUT = 30

# 缓存有效期（秒）
CACHE_TIMEOUT = 300  # 5分钟

# 每页显示的歌曲数量
SONGS_PER_PAGE = 5

# 清理过期的处理请求
def clean_expired_requests():
    """清理超时的处理请求"""
    current_time = time.time()
    expired_users = []
    
    for user_id, requests in PROCESSING_REQUESTS.items():
        expired_callbacks = []
        for callback_data, timestamp in requests.items():
            if current_time - timestamp > REQUEST_TIMEOUT:
                expired_callbacks.append(callback_data)
        
        for callback in expired_callbacks:
            requests.pop(callback, None)
        
        if not requests:
            expired_users.append(user_id)
    
    for user_id in expired_users:
        PROCESSING_REQUESTS.pop(user_id, None)

# 清理过期的缓存
def clean_expired_cache():
    """清理过期的搜索结果缓存"""
    current_time = time.time()
    expired_keys = []
    
    for key, cache_data in RESULTS_CACHE.items():
        if current_time - cache_data["timestamp"] > CACHE_TIMEOUT:
            expired_keys.append(key)
    
    for key in expired_keys:
        RESULTS_CACHE.pop(key, None)
    
    if expired_keys:
        logger.info(f"已清理 {len(expired_keys)} 条过期缓存，当前缓存数量: {len(RESULTS_CACHE)}")

# 清理过期的速率限制记录和冷却状态
def clean_rate_limits():
    """清理过期的速率限制记录和冷却状态"""
    current_time = time.time()
    
    # 清理速率限制记录
    for user_id in list(USER_RATE_LIMITS.keys()):
        USER_RATE_LIMITS[user_id] = [t for t in USER_RATE_LIMITS[user_id] if current_time - t < RATE_LIMIT_WINDOW]
        if not USER_RATE_LIMITS[user_id]:
            del USER_RATE_LIMITS[user_id]
    
    # 清理冷却状态
    expired_cooldowns = [user_id for user_id, cool_until in USER_COOLDOWNS.items() if current_time > cool_until]
    for user_id in expired_cooldowns:
        del USER_COOLDOWNS[user_id]

# 检查用户是否超过速率限制
def check_rate_limit(user_id):
    """
    检查用户是否超过速率限制
    返回: (是否允许请求, 冷却时间秒数或None)
    """
    current_time = time.time()
    
    # 检查用户是否在冷却状态
    if user_id in USER_COOLDOWNS:
        cool_until = USER_COOLDOWNS[user_id]
        if current_time < cool_until:
            return False, int(cool_until - current_time) + 1
    
    # 清理过期请求
    USER_RATE_LIMITS[user_id] = [t for t in USER_RATE_LIMITS[user_id] if current_time - t < RATE_LIMIT_WINDOW]
    
    # 检查是否超过速率限制
    if len(USER_RATE_LIMITS[user_id]) >= RATE_LIMIT_MAX_REQUESTS:
        # 设置冷却时间
        USER_COOLDOWNS[user_id] = current_time + RATE_LIMIT_COOLDOWN
        return False, RATE_LIMIT_COOLDOWN
    
    # 记录本次请求
    USER_RATE_LIMITS[user_id].append(current_time)
    return True, None

# 安全处理文本
def safe_text(text):
    """安全处理文本，防止HTML注入和特殊字符"""
    if text is None:
        return ""
    return html.escape(str(text))

def get_music_url(platform, song_id):
    """根据平台和歌曲ID生成音乐播放链接"""
    if platform in MUSIC_URL_FORMAT:
        return MUSIC_URL_FORMAT[platform].format(song_id)
    return None

@cooldown
async def music_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理/music命令，根据名称查询歌曲详细信息"""
    user_id = update.effective_user.id
    # 获取用户名，如果没有用户名则使用用户ID
    user_name = update.effective_user.username or str(user_id)
    user_mention = f"@{user_name}"
    
    # 检查是否有参数
    args = context.args
    
    # 如果有help参数或没有参数，显示帮助信息
    if not args or (args and args[0].lower() == "help"):
        await update.message.reply_text(
            HELP_TEXT,
            parse_mode="Markdown"
        )
        return
    
    # 检查用户是否注册
    if not await process_user.async_user_exists(user_id):
        await update.message.reply_text(
            "请先使用 /me 命令注册个人信息后再使用此功能。\n"
            "Please register first using the /me command before using this feature."
        )
        return
    
    # 检查用户速率限制
    allowed, cooldown_time = check_rate_limit(user_id)
    if not allowed:
        await update.message.reply_text(
            f"{user_mention} 您的搜索频率过快，请 {cooldown_time} 秒后再试。\n"
            f"Your search rate is too fast, please try again after {cooldown_time} seconds."
        )
        return
    
    # 获取用户提供的歌曲名称
    song_name = " ".join(args)
    
    # 发送处理中消息
    processing_msg = await update.message.reply_text(
        f"⏳ 正在搜索歌曲 \"{safe_text(song_name)}\"，请稍候...\n"
        f"Searching for song \"{safe_text(song_name)}\", please wait..."
    )
    
    try:
        # 默认搜索网易云音乐
        music_platform = "wy"
        page = 1
        limit = 20  # 增加获取数量以支持翻页
        
        # 生成缓存键
        cache_key = f"{song_name}_{music_platform}_{limit}"
        
        # 尝试从缓存获取结果
        if cache_key in RESULTS_CACHE and time.time() - RESULTS_CACHE[cache_key]["timestamp"] < CACHE_TIMEOUT:
            results = RESULTS_CACHE[cache_key]["data"]
            logger.info(f"从缓存获取搜索结果: {cache_key}")
        else:
            # 搜索歌曲信息
            results = await search_music(song_name, music_platform, page, limit)
            
            # 缓存结果
            if results and results.get("data"):
                RESULTS_CACHE[cache_key] = {
                    "data": results,
                    "timestamp": time.time()
                }
        
        if not results or not results.get("data") or len(results["data"]) == 0:
            # 如果没找到，尝试使用搜索名前几个词（可能用户输入了歌手）
            song_words = song_name.split()
            if len(song_words) > 1:
                # 尝试仅用前半部分词搜索
                half_length = max(1, len(song_words) // 2)
                shorter_name = " ".join(song_words[:half_length])
                
                # 更新正在处理的消息
                await processing_msg.edit_text(
                    f"⏳ 未找到精确匹配，正在尝试搜索 \"{safe_text(shorter_name)}\"..."
                )
                
                # 生成新的缓存键
                new_cache_key = f"{shorter_name}_{music_platform}_{limit}"
                
                # 尝试从缓存获取结果
                if new_cache_key in RESULTS_CACHE and time.time() - RESULTS_CACHE[new_cache_key]["timestamp"] < CACHE_TIMEOUT:
                    results = RESULTS_CACHE[new_cache_key]["data"]
                    logger.info(f"从缓存获取搜索结果: {new_cache_key}")
                else:
                    # 搜索歌曲信息
                    results = await search_music(shorter_name, music_platform, page, limit)
                    
                    # 缓存结果
                    if results and results.get("data"):
                        RESULTS_CACHE[new_cache_key] = {
                            "data": results,
                            "timestamp": time.time()
                        }
        
        if not results or not results.get("data") or len(results["data"]) == 0:
            await processing_msg.edit_text(
                f"{user_mention} 未找到与 \"{safe_text(song_name)}\" 相关的歌曲信息。\n"
                f"No song information related to \"{safe_text(song_name)}\" was found."
            )
            return
        
        # 准备回复消息
        songs = results["data"]
        current_page = 1
        total_pages = (len(songs) + SONGS_PER_PAGE - 1) // SONGS_PER_PAGE
        
        # 显示第一页
        await display_songs_page(
            update, context, processing_msg, songs, song_name, 
            music_platform, current_page, total_pages, user_mention
        )
        
        # 记录用户使用了该功能
        logger.info(f"用户 {user_name}(ID:{user_id}) 搜索了歌曲: {song_name}")
        
    except Exception as e:
        logger.error(f"搜索歌曲信息时出错: {str(e)}")
        await processing_msg.edit_text(
            f"{user_mention} 搜索歌曲信息时出错，请稍后再试。\n"
            f"Error: {str(e)}"
        )

async def display_songs_page(update, context, message, songs, song_name, platform, page, total_pages, user_mention=None):
    """显示分页的歌曲结果"""
    # 计算当前页的歌曲
    start_idx = (page - 1) * SONGS_PER_PAGE
    end_idx = min(start_idx + SONGS_PER_PAGE, len(songs))
    current_songs = songs[start_idx:end_idx]
    
    # 创建消息文本
    if user_mention:
        reply_text = f"{user_mention} 搜索结果 - \"{safe_text(song_name)}\"：\n\n"
    else:
        platform_name = PLATFORM_MAP.get(platform, platform)
        reply_text = f"搜索结果 - \"{safe_text(song_name)}\" ({platform_name})：\n\n"
    
    # 添加搜索结果
    for i, song in enumerate(current_songs, start=start_idx+1):
        platform_name = PLATFORM_MAP.get(song["type"], song["type"])
        music_url = get_music_url(song["type"], song["id"])
        
        reply_text += f"{i}. {safe_text(song['name'])}\n"
        reply_text += f"   👤 歌手：{safe_text(song['artist'])}\n"
        reply_text += f"   💿 专辑：{safe_text(song['album'])}\n"
        reply_text += f"   🎵 平台：{platform_name}\n"
        
        # 如果有链接，添加带超链接的ID，否则只显示ID
        if music_url:
            reply_text += f"   🆔 ID：<a href=\"{music_url}\">{song['id']}</a>\n\n"
        else:
            reply_text += f"   🆔 ID：{song['id']}\n\n"
    
    # 添加分页信息
    if total_pages > 1:
        reply_text += f"第 {page}/{total_pages} 页"
    
    # 创建平台选择按钮和翻页按钮
    keyboard = []
    
    # 添加翻页按钮（如果有多页）
    if total_pages > 1:
        page_buttons = []
        
        # 上一页按钮
        if page > 1:
            page_buttons.append(
                InlineKeyboardButton("◀️ 上一页", callback_data=f"music_page_{platform}_{song_name}_{page-1}")
            )
        
        # 当前页/总页数
        page_buttons.append(
            InlineKeyboardButton(f"{page}/{total_pages}", callback_data=f"music_info_page")
        )
        
        # 下一页按钮
        if page < total_pages:
            page_buttons.append(
                InlineKeyboardButton("下一页 ▶️", callback_data=f"music_page_{platform}_{song_name}_{page+1}")
            )
        
        keyboard.append(page_buttons)
    
    # 添加平台选择按钮
    platform_buttons = []
    
    for platform_code, platform_name in PLATFORM_MAP.items():
        if platform_code != platform:  # 不显示当前平台
            callback_data = f"music_{platform_code}_{song_name}_1"  # 添加页码
            platform_buttons.append(
                InlineKeyboardButton(platform_name, callback_data=callback_data)
            )
        
        # 每行3个按钮
        if len(platform_buttons) == 3:
            keyboard.append(platform_buttons)
            platform_buttons = []
    
    # 添加剩余按钮
    if platform_buttons:
        keyboard.append(platform_buttons)
    
    # 创建内联键盘标记
    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    
    # 更新消息
    await message.edit_text(
        reply_text,
        reply_markup=reply_markup,
        parse_mode="HTML",  # 添加HTML解析模式以支持超链接
        disable_web_page_preview=True  # 禁用链接预览
    )

async def music_platform_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理音乐平台选择的回调查询"""
    user_id = update.effective_user.id
    query = update.callback_query
    
    # 获取数据
    data = query.data
    
    # 如果是页码信息按钮，只提示用户
    if data == "music_info_page":
        await query.answer("当前页码/总页数", show_alert=False)
        return
    
    # 清理过期请求和缓存
    clean_expired_requests()
    clean_expired_cache()
    clean_rate_limits()
    
    # 检查用户速率限制
    allowed, cooldown_time = check_rate_limit(user_id)
    if not allowed:
        await query.answer(
            f"您的点击频率过快，请 {cooldown_time} 秒后再试。",
            show_alert=True
        )
        return
    
    # 检查用户是否有正在处理的相同请求
    if user_id in PROCESSING_REQUESTS and data in PROCESSING_REQUESTS[user_id]:
        # 如果有，告知用户请等待
        await query.answer("请等待当前搜索完成，不要重复点击", show_alert=True)
        return
    
    # 没有重复请求，记录当前请求
    if user_id not in PROCESSING_REQUESTS:
        PROCESSING_REQUESTS[user_id] = {}
    PROCESSING_REQUESTS[user_id][data] = time.time()
    
    try:
        # 处理翻页回调
        if data.startswith("music_page_"):
            await handle_page_callback(update, context, data)
            return
        
        # 处理平台选择回调
        parts = data.split("_", 3)
        
        if len(parts) < 3:
            await query.answer("无效的回调数据")
            return
        
        # 提取平台和歌曲名称
        platform = parts[1]
        song_name = parts[2]
        page = 1  # 默认页码
        
        # 如果有页码参数
        if len(parts) > 3 and parts[3].isdigit():
            page = int(parts[3])
        
        # 先回应回调查询，避免用户界面卡住
        await query.answer(f"正在搜索 {PLATFORM_MAP.get(platform, platform)} 的歌曲...")
        
        # 发送正在处理的消息
        await query.edit_message_text(
            f"⏳ 正在 {PLATFORM_MAP.get(platform, platform)} 上搜索 \"{safe_text(song_name)}\"，请稍候..."
        )
        
        # 生成缓存键
        limit = 20  # 保持与原始搜索一致
        cache_key = f"{song_name}_{platform}_{limit}"
        
        # 尝试从缓存获取结果
        if cache_key in RESULTS_CACHE and time.time() - RESULTS_CACHE[cache_key]["timestamp"] < CACHE_TIMEOUT:
            results = RESULTS_CACHE[cache_key]["data"]
            logger.info(f"从缓存获取搜索结果: {cache_key}")
        else:
            # 搜索歌曲信息
            results = await search_music(song_name, platform, 1, limit)
            
            # 缓存结果
            if results and results.get("data"):
                RESULTS_CACHE[cache_key] = {
                    "data": results,
                    "timestamp": time.time()
                }
        
        if not results or not results.get("data") or len(results["data"]) == 0:
            await query.edit_message_text(
                f"未在 {PLATFORM_MAP.get(platform, platform)} 上找到与 \"{safe_text(song_name)}\" 相关的歌曲信息。"
            )
            return
        
        # 准备回复消息
        songs = results["data"]
        total_pages = (len(songs) + SONGS_PER_PAGE - 1) // SONGS_PER_PAGE
        
        # 检查页码是否有效
        if page < 1:
            page = 1
        elif page > total_pages:
            page = total_pages
        
        # 显示指定页的歌曲
        await display_songs_page(
            update, context, query.message, songs, song_name, 
            platform, page, total_pages
        )
        
        # 记录用户使用了该功能
        user_name = update.effective_user.username or str(user_id)
        logger.info(f"用户 {user_name}(ID:{user_id}) 在 {platform} 平台搜索了歌曲: {song_name}")
        
    except Exception as e:
        logger.error(f"搜索歌曲信息时出错: {str(e)}")
        await query.edit_message_text(
            f"搜索歌曲信息时出错，请稍后再试。\n"
            f"Error: {str(e)}"
        )
    finally:
        # 无论成功与否，都清理请求记录
        if user_id in PROCESSING_REQUESTS:
            PROCESSING_REQUESTS[user_id].pop(data, None)
            if not PROCESSING_REQUESTS[user_id]:
                PROCESSING_REQUESTS.pop(user_id, None)

async def handle_page_callback(update, context, data):
    """处理翻页回调"""
    query = update.callback_query
    user_id = update.effective_user.id
    
    # 解析数据
    parts = data.split("_", 4)
    if len(parts) < 5:
        await query.answer("无效的翻页数据")
        return
    
    platform = parts[2]
    song_name = parts[3]
    try:
        page = int(parts[4])
    except ValueError:
        await query.answer("无效的页码")
        return
    
    # 检查用户速率限制（翻页操作使用相同的速率限制）
    allowed, cooldown_time = check_rate_limit(user_id)
    if not allowed:
        await query.answer(
            f"您的点击频率过快，请 {cooldown_time} 秒后再试。",
            show_alert=True
        )
        return
    
    # 先回应回调查询
    await query.answer(f"正在加载第 {page} 页...")
    
    # 生成缓存键
    limit = 20
    cache_key = f"{song_name}_{platform}_{limit}"
    
    # 尝试从缓存获取结果
    if cache_key in RESULTS_CACHE and time.time() - RESULTS_CACHE[cache_key]["timestamp"] < CACHE_TIMEOUT:
        results = RESULTS_CACHE[cache_key]["data"]
    else:
        # 如果缓存过期，重新搜索
        results = await search_music(song_name, platform, 1, limit)
        
        # 缓存结果
        if results and results.get("data"):
            RESULTS_CACHE[cache_key] = {
                "data": results,
                "timestamp": time.time()
            }
    
    if not results or not results.get("data") or len(results["data"]) == 0:
        await query.edit_message_text(
            f"未找到与 \"{safe_text(song_name)}\" 相关的歌曲信息。"
        )
        return
    
    # 准备回复消息
    songs = results["data"]
    total_pages = (len(songs) + SONGS_PER_PAGE - 1) // SONGS_PER_PAGE
    
    # 检查页码是否有效
    if page < 1:
        page = 1
    elif page > total_pages:
        page = total_pages
    
    # 显示指定页的歌曲
    await display_songs_page(
        update, context, query.message, songs, song_name, 
        platform, page, total_pages
    )

async def search_music(song_name, music_type="wy", page=1, limit=10):
    """调用API搜索歌曲信息"""
    # 参数安全处理
    safe_song_name = safe_text(song_name)
    safe_music_type = music_type if music_type in PLATFORM_MAP else "wy"
    safe_page = max(1, int(page) if str(page).isdigit() else 1)
    safe_limit = max(1, min(50, int(limit) if str(limit).isdigit() else 10))  # 限制最大值为50
    
    params = {
        "name": safe_song_name,
        "type": safe_music_type,
        "page": safe_page,
        "limit": safe_limit
    }
    
    try:
        # 使用超时控制防止长时间等待
        async with aiohttp.ClientSession() as session:
            async with session.get(MUSIC_API_URL, params=params, headers=HEADERS, timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    
                    # 检查API返回结果
                    if data.get("code") == 1:
                        return data
                    else:
                        logger.error(f"API返回错误: {data}")
                        return None
                else:
                    logger.error(f"API请求失败，状态码: {response.status}")
                    return None
    except aiohttp.ClientError as e:
        logger.error(f"连接API时出错: {str(e)}")
        return None
    except asyncio.TimeoutError:
        logger.error("请求API超时")
        return None
    except Exception as e:
        logger.error(f"搜索歌曲信息时出错: {str(e)}")
        return None

# 定期清理超时的处理请求任务
async def clean_expired_requests_job(context: ContextTypes.DEFAULT_TYPE):
    """定期清理超时的处理请求和缓存"""
    # 清理请求
    clean_expired_requests()
    # 清理缓存
    clean_expired_cache()
    # 清理速率限制记录和冷却状态
    clean_rate_limits()
    
    # 记录日志
    requests_count = sum(len(requests) for requests in PROCESSING_REQUESTS.values())
    rate_limits_count = sum(len(timestamps) for timestamps in USER_RATE_LIMITS.values())
    logger.info(
        f"已清理过期数据 - 请求数: {requests_count}, 缓存数: {len(RESULTS_CACHE)}, "
        f"速率限制记录数: {rate_limits_count}, 冷却状态数: {len(USER_COOLDOWNS)}"
    )

def setup_music_handlers(application):
    """设置music命令处理器"""
    application.add_handler(CommandHandler("music", music_command))
    application.add_handler(CallbackQueryHandler(music_platform_callback, pattern=r"^music_"))
    
    # 添加定期清理任务，每5分钟执行一次
    application.job_queue.run_repeating(clean_expired_requests_job, interval=300, first=10)
    
    logger.info("音乐搜索命令 (/music) 处理器已设置")
