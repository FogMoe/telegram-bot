import logging
import asyncio
import random
import aiohttp
import time
import json
from datetime import datetime, timedelta
from functools import lru_cache
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CommandHandler, ContextTypes, CallbackQueryHandler
from core import mysql_connection, process_user
from core.command_cooldown import cooldown

# 创建一个日志记录器
logger = logging.getLogger(__name__)

# Konachan API URLs（主要和备用）
KONACHAN_API_URL = "https://konachan.net/post.json"
KONACHAN_BACKUP_API_URL = "https://konachan.com/post.json"
YANDE_API_URL = "https://yande.re/post.json"  # 另一个备用API
COIN_COST = 5  # 使用/pic命令消耗的金币数量
HD_COIN_COST = 10  # 获取高清图片额外消耗的金币数量

# 请求头
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/96.0.4664.110 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "Cache-Control": "max-age=0"
}

# 图片缓存
IMAGE_CACHE = {
    "safe": [],  # 安全图片缓存
    "nsfw": [],  # 成人内容图片缓存
    "last_update": None  # 最后更新时间
}

# 全局图片数据缓存，用于高清图片功能
# 结构: {image_id: {'file_url': url, 'expires': datetime, 'tags': tags, 'stats': stats}}
HD_IMAGE_CACHE = {}

# 最大并发请求数控制
MAX_CONCURRENT_REQUESTS = 5
request_semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

# 帮助信息
HELP_TEXT = """
📷 **图片命令使用说明** 📷

基本命令:
• `/pic` - 随机获取一张图片，消耗5金币
• `/pic help` - 显示此帮助信息

高级选项:
• `/pic nsfw` - 获取成人内容图片，消耗5金币 (需要权限等级≥2)
• 点击高清图片按钮 - 获取原图，额外消耗10金币

注意事项:
• 所有图片均从公开图库随机获取
• 使用成人内容选项需要足够的权限
• 部分图片可能无法显示，金币将自动退还
"""

# 添加用户按钮点击状态跟踪和处理锁
# 格式为 {user_id: {image_id: timestamp}}
USER_HD_REQUESTS = {}

# 记录图片请求者，格式为 {image_id: user_id}
IMAGE_REQUESTERS = {}

# 正在处理或已处理的图片ID集合
PROCESSING_IMAGES = set()

# 用户查看帮助记录，格式: {user_id: last_help_time}
USER_HELP_RECORDS = {}

# 用户最近查看过的图片记录，避免短期内重复
# 格式: {user_id: {image_id: timestamp, ...}}
USER_RECENT_IMAGES = {}

# 全局最近发送的图片ID集合，防止频繁重复的图片
RECENT_SENT_IMAGES = set()
# 全局最近发送图片的最大数量
MAX_RECENT_IMAGES = 100

# 清理过期图片数据的函数
def clean_expired_images():
    """清理过期的高清图片数据缓存"""
    global HD_IMAGE_CACHE
    now = datetime.now()
    expired_keys = [k for k, v in HD_IMAGE_CACHE.items() if now > v.get('expires', now)]
    for key in expired_keys:
        HD_IMAGE_CACHE.pop(key, None)
    logger.info(f"清理了 {len(expired_keys)} 条过期图片数据，当前缓存图片数量: {len(HD_IMAGE_CACHE)}")

# 格式化图片标签和统计信息
def format_image_info(image_data):
    """格式化图片的标签和统计信息"""
    info = []
    
    # 添加标签信息
    if 'tags' in image_data and image_data['tags']:
        tags = image_data['tags'].split()[:10]  # 统一都最多显示10个标签
        if tags:
            formatted_tags = ' '.join([f"#{tag}" for tag in tags])
            info.append(f"标签: {formatted_tags}")
    
    # 添加统计信息
    stats = []
    if 'width' in image_data and 'height' in image_data:
        stats.append(f"分辨率: {image_data.get('width')}x{image_data.get('height')}")
    
    if 'file_size' in image_data:
        size_mb = image_data.get('file_size', 0) / (1024 * 1024)
        stats.append(f"文件大小: {size_mb:.2f}MB")
    
    if 'score' in image_data:
        stats.append(f"评分: {image_data.get('score')}")
    
    if stats:
        info.append("统计信息: " + ", ".join(stats))
    
    return "\n".join(info)

@cooldown
async def pic_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理/pic命令，消耗金币发送随机图片"""
    user_id = update.effective_user.id
    # 获取用户名，如果没有用户名则使用用户ID
    user_name = update.effective_user.username or str(user_id)
    user_mention = f"@{user_name}"
    
    # 检查是否有参数
    args = context.args
    
    # 如果有help参数，显示帮助信息
    if args and args[0].lower() == "help":
        # 记录用户查看了帮助信息
        USER_HELP_RECORDS[user_id] = datetime.now()
        
        await update.message.reply_text(
            HELP_TEXT,
            parse_mode="Markdown"  # 使用Markdown格式
        )
        return
    
    # 检查用户是否需要查看帮助（24小时内第一次使用）
    now = datetime.now()
    if user_id not in USER_HELP_RECORDS or (now - USER_HELP_RECORDS[user_id]).total_seconds() > 86400:
        # 显示帮助信息并记录
        USER_HELP_RECORDS[user_id] = now
        await update.message.reply_text(
            f"{user_mention} 这是您24小时内首次使用图片命令，以下是帮助信息：\n\n" + HELP_TEXT,
            parse_mode="Markdown"
        )
        return
    
    # 决定是否获取NSFW内容
    is_nsfw = args and args[0].lower() == "nsfw"
    
    # 检查用户是否注册
    if not await process_user.async_user_exists(user_id):
        await update.message.reply_text(
            "请先使用 /me 命令注册个人信息后再使用此功能。\n"
            "Please register first using the /me command before using this feature."
        )
        return
    
    # 如果是NSFW内容，检查用户权限
    if is_nsfw:
        user_permission = await process_user.async_get_user_permission(user_id)
        if user_permission < 2:
            await update.message.reply_text(
                "您的权限等级不足，需要权限等级≥2才能使用NSFW选项。\n"
                "Your permission level is not enough. You need permission level ≥2 to use NSFW option.\n"
                "您可以前往商城 /shop 购买权限等级，权限等级越高，您可以使用的功能越多。\n"
                "You can purchase permission levels in the /shop to increase your usage of features."
            )
            return
    
    # 获取用户金币数量
    user_coins = await process_user.async_get_user_coins(user_id)
    
    # 检查用户金币是否足够
    if user_coins < COIN_COST:
        await update.message.reply_text(
            f"{user_mention} 您的金币不足！使用此功能需要 {COIN_COST} 个金币，您当前有 {user_coins} 个金币。\n"
            f"Not enough coins! This feature requires {COIN_COST} coins, you have {user_coins} coins."
        )
        return
    
    # 发送处理中消息
    processing_msg = await update.message.reply_text(
        "⏳ 正在获取图片，请稍候...\n"
        "Fetching image, please wait..."
    )
    
    try:
        # 扣除用户金币
        await process_user.async_update_user_coins(user_id, -COIN_COST)
        
        # 获取随机图片，并避免用户最近看过的图片
        image_data = await get_random_image(is_nsfw, user_id)
        
        if not image_data:
            # 如果获取图片失败，退还金币
            await process_user.async_update_user_coins(user_id, COIN_COST)
            await processing_msg.edit_text(
                f"{user_mention} 获取图片失败，请稍后再试。金币已退还。\n"
                "Failed to fetch image. Please try again later. Your coins have been refunded."
            )
            return
        
        # 获取图片URL和高清版URL
        sample_url = image_data.get('sample_url')
        file_url = image_data.get('file_url')
        
        # 确保图片有ID，如果没有则生成一个随机ID
        if 'id' not in image_data or not image_data['id']:
            image_data['id'] = f"img_{int(time.time())}_{random.randint(1000, 9999)}"
        
        image_id = str(image_data['id'])  # 确保ID是字符串类型
        
        # 记录图片请求者，用于群组中按钮点击权限控制
        IMAGE_REQUESTERS[image_id] = user_id
        
        # 如果都没有有效URL，退还金币并返回错误
        if not sample_url and not file_url:
            await process_user.async_update_user_coins(user_id, COIN_COST)
            await processing_msg.edit_text(
                f"{user_mention} 获取图片URL失败，请稍后再试。金币已退还。\n"
                "Failed to get image URL. Please try again later. Your coins have been refunded."
            )
            return
        
        # 优先使用sample_url（缩略图）
        image_url = sample_url or file_url
        
        # 创建内联键盘，添加高清版按钮
        keyboard = []
        if file_url and file_url != image_url:
            # 使用唯一标识符作为回调数据
            callback_data = f"pic_hd_{image_id}"
            keyboard.append([
                InlineKeyboardButton(f"查看高清原图 ({HD_COIN_COST}金币)", callback_data=callback_data)
            ])
        
        reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
        
        # 构建图片信息文本
        info_text = f"{user_mention} 消耗了 {COIN_COST} 金币获取此图片。\n"
        if is_nsfw:
            info_text += "类型: NSFW\n"
        
        # 添加图片标签和统计信息
        img_details = format_image_info(image_data)
        if img_details:
            info_text += f"\n{img_details}\n"
        
        if reply_markup:
            info_text += f"\n点击下方按钮可获取高清原图，需额外消耗 {HD_COIN_COST} 金币。"
        
        # 清理过期缓存数据
        clean_expired_images()
        
        # 保存图片数据到全局缓存，以便后续高清图片请求使用
        HD_IMAGE_CACHE[image_id] = {
            'file_url': file_url,
            'expires': datetime.now() + timedelta(minutes=30),  # 30分钟后过期
            'tags': image_data.get('tags', ''),
            'stats': {
                'width': image_data.get('width'),
                'height': image_data.get('height'),
                'file_size': image_data.get('file_size'),
                'score': image_data.get('score')
            }
        }
        
        # 记录存储的高清图片数据
        logger.info(f"已存储高清图片数据: ID={image_id}, URL={file_url}")
        logger.info(f"当前缓存的图片ID数量: {len(HD_IMAGE_CACHE)}")
        
        # 记录用户最近看过的图片
        if user_id not in USER_RECENT_IMAGES:
            USER_RECENT_IMAGES[user_id] = {}
        # 记录图片ID和时间戳
        USER_RECENT_IMAGES[user_id][image_id] = datetime.now()
        
        # 同时记录到全局最近发送图片集合
        RECENT_SENT_IMAGES.add(image_id)
        # 如果全局集合超过最大限制，删除最旧的
        if len(RECENT_SENT_IMAGES) > MAX_RECENT_IMAGES:
            # 由于集合无序，随机删除一个元素
            try:
                RECENT_SENT_IMAGES.pop()
            except KeyError:
                pass
        
        # 发送图片，回复用户的原始命令
        sent_message = await context.bot.send_photo(
            chat_id=update.effective_chat.id,
            photo=image_url,
            caption=info_text,
            reply_markup=reply_markup,
            has_spoiler=is_nsfw,  # 如果是NSFW内容，启用spoiler效果
            reply_to_message_id=update.message.message_id  # 回复用户的原始命令
        )
        
        # 保存消息ID到缓存，以便高清回调使用
        if image_id and reply_markup:
            HD_IMAGE_CACHE[image_id]['message_id'] = sent_message.message_id
        
        # 删除处理中消息
        await processing_msg.delete()
        
        # 记录日志
        logger.info(f"用户 {user_name}(ID:{user_id}) 消耗 {COIN_COST} 金币获取了一张{'NSFW' if is_nsfw else '普通'}图片")
        
    except Exception as e:
        # 处理异常，退还金币
        logger.error(f"发送图片时出错: {str(e)}")
        await process_user.async_update_user_coins(user_id, COIN_COST)
        await processing_msg.edit_text(
            f"{user_mention} 发送图片时出错，请稍后再试。金币已退还。\n"
            "Error sending image. Please try again later. Your coins have been refunded."
        )

async def hd_pic_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理高清图片按钮回调"""
    query = update.callback_query
    
    user_id = update.effective_user.id
    user_name = update.effective_user.username or str(user_id)
    user_mention = f"@{user_name}"
    
    # 添加一个完成标志，确保不会同时发送多个回复
    processing_completed = False
    
    # 解析回调数据
    try:
        _, action, image_id = query.data.split('_', 2)
        logger.info(f"高清图片请求: action={action}, image_id={image_id}, 用户ID={user_id}")
        
        if action != 'hd':
            logger.warning(f"无效的操作类型: {action}")
            await query.answer("无效的操作类型", show_alert=True)
            return
    except ValueError as e:
        logger.error(f"解析回调数据出错: {str(e)}, data={query.data}")
        await query.answer("无效的请求数据", show_alert=True)
        return
    
    # 使用一个原子操作检查并添加图片ID到处理集合
    # 如果图片ID已经在处理集合中，立即拒绝此请求
    if image_id in PROCESSING_IMAGES:
        logger.warning(f"图片 {image_id} 已经在处理中，拒绝用户 {user_id} 的请求")
        await query.answer("此图片正在处理中或已被获取，请勿重复点击", show_alert=True)
        return
    
    # 立即将图片ID添加到处理集合中，防止并发请求
    PROCESSING_IMAGES.add(image_id)
    
    try:
        # 检查全局缓存中是否存在图片数据
        if image_id not in HD_IMAGE_CACHE:
            logger.warning(f"图片ID {image_id} 在缓存中不存在，可用ID数量: {len(HD_IMAGE_CACHE)}")
            await query.answer("图片数据已过期，请重新获取", show_alert=True)
            # 从处理集合中移除图片ID，允许用户稍后重试
            PROCESSING_IMAGES.discard(image_id)
            return
        
        # 获取图片数据
        pic_data = HD_IMAGE_CACHE[image_id]
        logger.info(f"找到图片数据: {pic_data}")
        
        # 检查数据是否过期
        now = datetime.now()
        if 'expires' in pic_data and now > pic_data['expires']:
            logger.warning(f"图片数据已过期: 当前时间={now}, 过期时间={pic_data['expires']}")
            await query.answer("图片数据已过期，请重新获取", show_alert=True)
            # 删除过期数据
            HD_IMAGE_CACHE.pop(image_id, None)
            # 从处理集合中移除图片ID，允许用户稍后重试
            PROCESSING_IMAGES.discard(image_id)
            return
        
        # 获取高清图片URL
        hd_url = pic_data.get('file_url')
        if not hd_url:
            logger.warning(f"图片数据中不存在file_url字段: {pic_data}")
            await query.answer("高清图片不可用", show_alert=True)
            # 从处理集合中移除图片ID，允许用户稍后重试
            PROCESSING_IMAGES.discard(image_id)
            return
        
        logger.info(f"高清图片URL: {hd_url}")
        
        # 获取用户金币数量
        user_coins = await process_user.async_get_user_coins(user_id)
        
        # 检查用户金币是否足够
        if user_coins < HD_COIN_COST:
            await query.answer(
                f"金币不足！查看高清图片需要 {HD_COIN_COST} 个金币，您当前有 {user_coins} 个金币。",
                show_alert=True
            )
            # 关键修复：从处理集合中移除图片ID，允许用户在金币充足后重试
            logger.info(f"用户 {user_id} 金币不足，从处理集合中移除图片 {image_id}")
            PROCESSING_IMAGES.discard(image_id)
            return
        
        # 检查图片大小
        file_size_mb = 0
        if 'stats' in pic_data and 'file_size' in pic_data['stats'] and pic_data['stats']['file_size']:
            file_size_mb = pic_data['stats']['file_size'] / (1024 * 1024)
            logger.info(f"高清图片大小: {file_size_mb:.2f}MB")
        
        # 通知用户请求已接受
        await query.answer("正在处理您的高清图片请求...")
        
        # 立即更新原消息，移除按钮并更新文本
        original_caption = query.message.caption
        # 替换"您"为用户@提及
        update_text = f"{original_caption.split('点击下方按钮')[0]}\n{user_mention} 消耗了 {HD_COIN_COST} 金币获取此高清图片。"
        
        try:
            await query.edit_message_caption(
                caption=update_text,
                reply_markup=None
            )
        except Exception as e:
            logger.warning(f"更新原消息失败: {str(e)}")
        
        # 扣除用户金币
        await process_user.async_update_user_coins(user_id, -HD_COIN_COST)
        
        # 获取图片文件名和消息ID
        file_name = hd_url.split('/')[-1].split('?')[0]  # 提取URL中的文件名部分
        reply_to_message_id = query.message.message_id  # 回复原图片消息
        
        # 检查是否为NSFW内容
        is_nsfw = False
        if 'tags' in pic_data and pic_data['tags']:
            # 如果标签中包含常见NSFW相关标签，判断为NSFW
            nsfw_tags = ['nsfw', 'nude', 'naked', 'nipples', 'breasts', 'pussy', 
                        'questionable', 'explicit', 'sex', 'censored', 'uncensored']
            
            # 修复逻辑错误：正确检查图片标签是否包含在NSFW标签列表中
            image_tags_lower = [tag.lower() for tag in pic_data['tags'].split()]
            is_nsfw = any(nsfw_tag in image_tags_lower for nsfw_tag in nsfw_tags)
            
            # 额外检查：如果图片来源于NSFW请求，也标记为NSFW
            if 'source_is_nsfw' in pic_data and pic_data['source_is_nsfw']:
                is_nsfw = True
                
            logger.info(f"NSFW检测结果: {is_nsfw}, 图片标签: {pic_data['tags']}")
        
        # 准备图片描述
        caption_text = f"{user_mention} 消耗了 {HD_COIN_COST} 金币获取此高清图片" + (" (NSFW内容)" if is_nsfw else "")
        
        # 添加下载图片前的日志
        logger.info(f"开始从{hd_url}下载高清图片")
        
        # 更严格的超时控制，采用更可靠的下载方式
        try:
            # 使用更短的超时时间进行图片下载
            async with aiohttp.ClientSession() as session:
                # 设置总超时时间（包括连接、读取等）- 较大图片给予足够但有限的时间
                timeout = aiohttp.ClientTimeout(total=30)  # 30秒总超时
                
                async with session.get(hd_url, headers=HEADERS, timeout=timeout) as response:
                    if response.status == 200:
                        # 设置读取数据的超时，一旦连接成功但下载太慢，也会触发超时
                        content = await asyncio.wait_for(response.read(), timeout=25)
                        logger.info(f"成功下载图片，大小: {len(content)/1024/1024:.2f}MB")
                        
                        # 使用BytesIO发送文件
                        from io import BytesIO
                        file_obj = BytesIO(content)
                        file_obj.name = file_name
                        
                        await context.bot.send_document(
                            chat_id=update.effective_chat.id,
                            document=file_obj,
                            filename=file_name,
                            caption=caption_text,
                            reply_to_message_id=reply_to_message_id
                        )
                        
                        # 标记处理已完成
                        processing_completed = True
                        logger.info(f"用户 {user_name}(ID:{user_id}) 消耗 {HD_COIN_COST} 金币获取了一张高清图片")
                    else:
                        raise Exception(f"下载图片失败，HTTP状态码: {response.status}")
                        
        except asyncio.TimeoutError:
            logger.error(f"下载图片超时，URL: {hd_url}")
            raise Exception("下载图片超时，请尝试使用备用链接")
        except Exception as download_error:
            logger.error(f"下载图片失败: {str(download_error)}")
            raise download_error
            
    except Exception as e:
        # 处理异常，如果还没有成功发送，提供备用链接
        logger.error(f"发送高清图片时出错: {str(e)}")
        
        if not processing_completed:  # 只有在没有成功发送的情况下提供备用链接
            try:
                keyboard = [[InlineKeyboardButton("下载高清原图", url=hd_url)]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=f"{user_mention} 发送高清图片失败，您可以通过以下链接下载。\n图片大小: {file_size_mb:.2f}MB",
                    reply_markup=reply_markup,
                    reply_to_message_id=reply_to_message_id
                )
                
                # 提供链接成功，也标记为完成
                processing_completed = True
                logger.info(f"成功提供高清图片链接给用户 {user_id}")
                
            except Exception as inner_e:
                logger.error(f"提供高清图片链接也失败: {str(inner_e)}")
                
                # 如果所有尝试都失败，退还金币
                await process_user.async_update_user_coins(user_id, HD_COIN_COST)
                await query.answer("发送高清图片时出错，请稍后再试。您的金币已退还。", show_alert=True)
        
    finally:
        # 记录处理结果
        if processing_completed:
            logger.info(f"图片 {image_id} 请求已成功处理完成")
        else:
            # 只有当没有成功处理且没有明确返回（如金币不足）时才记录处理失败
            if image_id in PROCESSING_IMAGES and not any(text in str(e) for text in ["金币不足", "图片数据已过期", "高清图片不可用"]):
                logger.warning(f"图片 {image_id} 处理失败，已退还金币")
                # 如果处理未完成且尚未退还金币，确保退还
                try:
                    await process_user.async_update_user_coins(user_id, HD_COIN_COST)
                except Exception as refund_error:
                    logger.error(f"退还金币失败: {str(refund_error)}")

async def fetch_and_cache_images(is_nsfw=False, max_retries=3):
    """获取并缓存图片数据"""
    cache_key = "nsfw" if is_nsfw else "safe"
    
    # 如果缓存还有足够数据，直接返回
    if IMAGE_CACHE[cache_key] and len(IMAGE_CACHE[cache_key]) > 20:  # 增加保留图片数量从10到20
        # 随机混洗以避免总是从头取
        images_copy = IMAGE_CACHE[cache_key].copy()  # 创建副本再混洗，避免修改原始数据
        random.shuffle(images_copy)
        return images_copy
    
    # 设置API参数 - 增加获取图片数量
    params = {
        'limit': 200,  # 增加一次获取的图片数量
        'tags': 'rating:questionable' if is_nsfw else 'rating:safe',
        'order': 'random'
    }
    
    # 依次尝试不同的API
    api_urls = [KONACHAN_API_URL, KONACHAN_BACKUP_API_URL, YANDE_API_URL]
    images = []
    
    async with request_semaphore:  # 限制并发请求数
        for api_url in api_urls:
            retries = 0
            while retries < max_retries:
                try:
                    logger.info(f"尝试从 {api_url} 获取{'NSFW' if is_nsfw else '普通'}图片")
                    
                    async with aiohttp.ClientSession() as session:
                        async with session.get(api_url, params=params, headers=HEADERS, timeout=10) as response:
                            if response.status == 200:
                                data = await response.json()
                                
                                if data and isinstance(data, list) and len(data) > 0:
                                    # 确保每个图片对象都有id字段（如果API返回没有，使用MD5或随机生成）
                                    valid_images = []
                                    for img in data:
                                        if img.get('sample_url') or img.get('file_url'):
                                            # 如果没有id，使用md5或创建虚拟id
                                            if not img.get('id'):
                                                img['id'] = img.get('md5', str(random.randint(10000, 99999)))
                                            valid_images.append(img)
                                    
                                    if valid_images:
                                        images = valid_images
                                        logger.info(f"成功从 {api_url} 获取到 {len(valid_images)} 张图片")
                                        # 更新缓存 - 使用深拷贝避免引用问题
                                        IMAGE_CACHE[cache_key] = [img.copy() for img in images]
                                        IMAGE_CACHE["last_update"] = datetime.now()
                                        return images
                            else:
                                logger.error(f"API {api_url} 请求失败，状态码: {response.status}")
                                
                    retries += 1
                    # 如果API请求失败，等待一小段时间再重试
                    await asyncio.sleep(1)
                    
                except aiohttp.ClientError as e:
                    logger.error(f"连接 {api_url} 时出错: {str(e)}")
                    retries += 1
                    await asyncio.sleep(1)
                except asyncio.TimeoutError:
                    logger.error(f"请求 {api_url} 超时")
                    retries += 1
                    await asyncio.sleep(1)
                except Exception as e:
                    logger.error(f"从 {api_url} 获取图片时出错: {str(e)}")
                    retries += 1
                    await asyncio.sleep(1)
    
    # 如果API请求都失败，但缓存中有旧数据，返回旧数据的副本
    if IMAGE_CACHE[cache_key]:
        logger.warning("所有API请求都失败，使用缓存的图片数据")
        return IMAGE_CACHE[cache_key].copy()
    
    # 如果缓存中也没有数据，返回备用的静态图片列表
    logger.warning("所有API请求都失败且没有缓存，使用备用图片")
    
    # 备用的静态图片列表
    backup_images = []
    if is_nsfw:
        # NSFW备用图片
        backup_images = [
            {"id": "backup1", "sample_url": "https://konachan.net/sample/9ef08c3e40591a6d118edbd5a36b534f/Konachan.com%20-%20341083%20sample.jpg", "file_url": "https://konachan.net/image/9ef08c3e40591a6d118edbd5a36b534f/Konachan.com%20-%20341083%20anthropomorphism%20azur_lane%20breasts%20brown_eyes.jpg"},
            {"id": "backup2", "sample_url": "https://konachan.net/sample/3c1ac17a13b9214d26fec2ad9683f425/Konachan.com%20-%20340831%20sample.jpg", "file_url": "https://konachan.net/image/3c1ac17a13b9214d26fec2ad9683f425/Konachan.com%20-%20340831%20anthropomorphism%20aqua_eyes%20azur_lane.jpg"},
            {"id": "backup3", "sample_url": "https://konachan.net/sample/9aea3517d7eae0efd509c7a495e96c5e/Konachan.com%20-%20340619%20sample.jpg", "file_url": "https://konachan.net/image/9aea3517d7eae0efd509c7a495e96c5e/Konachan.com%20-%20340619%20animal_ears%20anthropomorphism%20blush.jpg"}
        ]
    else:
        # 安全的备用图片
        backup_images = [
            {"id": "backup1", "sample_url": "https://konachan.net/sample/e2739d73cde2f5e6f70ece824838247e/Konachan.com%20-%20341231%20sample.jpg", "file_url": "https://konachan.net/image/e2739d73cde2f5e6f70ece824838247e/Konachan.com%20-%20341231%20animal%20bird%20fish%20nobody%20original%20scenic%20signed%20sunset%20water.jpg"},
            {"id": "backup2", "sample_url": "https://konachan.net/sample/c76f10765c5a35c0af224a7607fb767a/Konachan.com%20-%20340969%20sample.jpg", "file_url": "https://konachan.net/image/c76f10765c5a35c0af224a7607fb767a/Konachan.com%20-%20340969%20animal%20bird%20cat%20grass%20nobody%20original%20tree.jpg"},
            {"id": "backup3", "sample_url": "https://konachan.net/sample/7d55c50f3afa25ff64223c7ef5dc81e7/Konachan.com%20-%20339980%20sample.jpg", "file_url": "https://konachan.net/image/7d55c50f3afa25ff64223c7ef5dc81e7/Konachan.com%20-%20339980%20landscape%20night%20nobody%20original%20scenic%20stars%20sunset%20tree.jpg"},
            {"id": "backup4", "sample_url": "https://konachan.net/sample/73f3713158e732d4a1bea0687d02f032/Konachan.com%20-%20339848%20sample.jpg", "file_url": "https://konachan.net/image/73f3713158e732d4a1bea0687d02f032/Konachan.com%20-%20339848%20animal%20bird%20forest%20nobody%20original%20scenic%20signed%20sunset%20tree.jpg"},
            {"id": "backup5", "sample_url": "https://konachan.net/sample/1e7218fb43b935a13b1df56640a3a646/Konachan.com%20-%20338774%20sample.jpg", "file_url": "https://konachan.net/image/1e7218fb43b935a13b1df56640a3a646/Konachan.com%20-%20338774%20clouds%20nobody%20original%20scenic%20signed%20sky%20sunset%20tree.jpg"}
        ]
    
    # 更新缓存
    IMAGE_CACHE[cache_key] = backup_images.copy()  # 使用副本避免引用问题
    IMAGE_CACHE["last_update"] = datetime.now()
    
    return backup_images

async def get_random_image(is_nsfw=False, user_id=None):
    """从API获取随机图片数据，避免用户最近看过的图片"""
    try:
        # 获取缓存的图片列表
        images = await fetch_and_cache_images(is_nsfw)
        
        if not images:
            return None
        
        # 过滤掉用户最近看过的图片
        filtered_images = images
        if user_id and user_id in USER_RECENT_IMAGES and USER_RECENT_IMAGES[user_id]:
            recent_ids = set(USER_RECENT_IMAGES[user_id].keys())
            filtered_images = [img for img in images if str(img.get('id', '')) not in recent_ids]
            
            # 如果过滤后没有可用图片，则仍使用完整列表，但优先使用最不常见的图片
            if not filtered_images:
                logger.info(f"用户 {user_id} 的过滤后图片列表为空，使用完整列表")
                # 但尽量避免全局最近发送过的图片
                filtered_images = [img for img in images if str(img.get('id', '')) not in RECENT_SENT_IMAGES]
                if not filtered_images:
                    filtered_images = images
        
        # 如果仍然有多个可选图片，随机选择一张
        if filtered_images:
            image = random.choice(filtered_images)
        else:
            # 理论上不应该到这里，但以防万一
            image = random.choice(images)
        
        # 从缓存中移除已使用的图片，避免重复
        cache_key = "nsfw" if is_nsfw else "safe"
        
        # 修复：使用更安全的方式移除缓存中的图片
        if IMAGE_CACHE[cache_key]:
            try:
                # 使用图片ID安全地移除（ID在fetch_and_cache_images中已确保存在）
                image_id = image.get("id")
                if image_id:
                    # 创建新的过滤后列表
                    IMAGE_CACHE[cache_key] = [img for img in IMAGE_CACHE[cache_key] 
                                             if img.get("id") != image_id]
            except Exception as e:
                # 如果移除过程出错，记录错误但不中断流程
                logger.warning(f"从缓存移除图片时出错: {str(e)}")
        
        return image
    except Exception as e:
        logger.error(f"获取随机图片时出错: {str(e)}")
        return None

# 定期刷新缓存的任务
async def refresh_cache_job(context):
    """定期刷新图片缓存"""
    try:
        logger.info("开始定期刷新图片缓存")
        
        # 后台获取普通图片和NSFW图片
        await fetch_and_cache_images(is_nsfw=False)
        await fetch_and_cache_images(is_nsfw=True)
        
        logger.info("图片缓存刷新完成")
    except Exception as e:
        logger.error(f"刷新图片缓存时出错: {str(e)}")

def setup_pic_handlers(application):
    """设置图片命令的处理器"""
    # 添加命令处理器
    application.add_handler(CommandHandler("pic", pic_command))
    
    # 添加回调查询处理器，处理高清图片按钮
    application.add_handler(CallbackQueryHandler(hd_pic_callback, pattern=r"^pic_hd_"))
    
    # 添加定期刷新缓存的任务，每30分钟执行一次
    application.job_queue.run_repeating(refresh_cache_job, interval=1800, first=10)
    
    # 添加定期清理过期图片请求记录的任务
    application.job_queue.run_repeating(
        lambda ctx: asyncio.create_task(clean_expired_requests(ctx)), 
        interval=3600,  # 每小时运行一次
        first=1800      # 首次运行在30分钟后
    )
    
    # 日志记录
    logger.info("图片系统已初始化")

async def clean_expired_requests(context):
    """清理过期的用户请求记录和图片请求者记录"""
    try:
        now = datetime.now()
        # 清理过期的用户请求记录
        for user_id in list(USER_HD_REQUESTS.keys()):
            for image_id in list(USER_HD_REQUESTS[user_id].keys()):
                request_time = USER_HD_REQUESTS[user_id][image_id]
                if (now - request_time).total_seconds() > 3600:  # 1小时后过期
                    del USER_HD_REQUESTS[user_id][image_id]
            # 如果用户没有任何请求记录，删除该用户的记录
            if not USER_HD_REQUESTS[user_id]:
                del USER_HD_REQUESTS[user_id]
        
        # 清理过期的图片请求者记录
        for image_id in list(IMAGE_REQUESTERS.keys()):
            # 如果图片ID不在高清图片缓存中，说明已过期
            if image_id not in HD_IMAGE_CACHE:
                del IMAGE_REQUESTERS[image_id]
        
        # 清理过期的处理图片记录（只保留在高清图片缓存中的ID）
        for image_id in list(PROCESSING_IMAGES):
            if image_id not in HD_IMAGE_CACHE:
                PROCESSING_IMAGES.remove(image_id)
        
        # 清理超过48小时的帮助记录
        for user_id in list(USER_HELP_RECORDS.keys()):
            help_time = USER_HELP_RECORDS[user_id]
            if (now - help_time).total_seconds() > 172800:  # 48小时后过期
                del USER_HELP_RECORDS[user_id]
        
        # 清理超过24小时的用户最近图片记录
        for user_id in list(USER_RECENT_IMAGES.keys()):
            for image_id in list(USER_RECENT_IMAGES[user_id].keys()):
                view_time = USER_RECENT_IMAGES[user_id][image_id]
                if (now - view_time).total_seconds() > 86400:  # 24小时后过期
                    del USER_RECENT_IMAGES[user_id][image_id]
            # 如果用户没有任何最近图片记录，删除该用户的记录
            if not USER_RECENT_IMAGES[user_id]:
                del USER_RECENT_IMAGES[user_id]
        
        logger.info(f"清理完成: 用户请求记录数量={len(USER_HD_REQUESTS)}, 图片请求者记录数量={len(IMAGE_REQUESTERS)}, "
                   f"处理中图片数量={len(PROCESSING_IMAGES)}, 帮助记录数量={len(USER_HELP_RECORDS)}, "
                   f"用户最近图片记录数量={len(USER_RECENT_IMAGES)}, 全局最近图片数量={len(RECENT_SENT_IMAGES)}")
    except Exception as e:
        logger.error(f"清理过期请求记录时出错: {str(e)}")
