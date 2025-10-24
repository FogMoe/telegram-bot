import logging
import asyncio
import aiohttp
import re
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes
import process_user
from command_cooldown import cooldown

# 创建一个日志记录器
logger = logging.getLogger(__name__)

# API URL
SHARE_LEAK_API_URL = "https://tools.mgtv100.com/external/v1/pear/privateShare"

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
🔍 **隐私链接检测使用说明** 🔍

基本命令:
• `/sf <链接>` - 检测分享链接是否泄露隐私
• `/sf help` - 显示此帮助信息

支持平台：
小红书、微博、网易云音乐、QQ音乐、全民K歌、喜马拉雅、
雪球、Keep、哔哩哔哩、百度、酷安、知乎、小宇宙、
汽水音乐、知识星球、即刻等

注意事项:
• 检测结果仅供参考
"""

# 简单的URL正则表达式
URL_PATTERN = re.compile(r'^(https?://)[a-zA-Z0-9\-\.]+\.[a-zA-Z]{2,}(/\S*)?$')

@cooldown
async def sf_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理/sf命令，检查分享链接是否泄露隐私"""
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
    
    # 获取用户提供的链接
    share_url = args[0]
    
    # 检查输入是否是链接
    if not URL_PATTERN.match(share_url):
        await update.message.reply_text(
            f"{user_mention} 请输入有效的链接格式。例如：https://example.com\n"
            "Please enter a valid link format. For example: https://example.com"
        )
        return
    
    # 发送处理中消息
    processing_msg = await update.message.reply_text(
        "⏳ 正在检测链接，请稍候...\n"
        "Checking link, please wait..."
    )
    
    try:
        # 检查链接是否泄露隐私
        result = await check_share_link(share_url)
        
        if result is None:
            await processing_msg.edit_text(
                f"{user_mention} 检测链接失败，请稍后再试。\n"
                "Failed to check link. Please try again later."
            )
            return
        
        # 准备回复消息
        reply_text = f"{user_mention} 链接检测结果：\n\n"
        
        if result == "该分享链接安全":
            reply_text += "✅ 您的分享链接安全，未检测到泄露个人隐私信息。"
        else:
            reply_text += f"⚠️ {result}"
        
        # 更新处理消息
        await processing_msg.edit_text(reply_text)
        
        # 记录用户使用了该功能
        logger.info(f"用户 {user_name}(ID:{user_id}) 检测了链接: {share_url}")
        
    except Exception as e:
        logger.error(f"检测链接时出错: {str(e)}")
        await processing_msg.edit_text(
            f"{user_mention} 检测链接时出错，请稍后再试。\n"
            f"Error: {str(e)}"
        )

async def check_share_link(share_url):
    """调用API检查分享链接是否泄露隐私"""
    params = {
        "share_url": share_url
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(SHARE_LEAK_API_URL, json=params, headers=HEADERS, timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    
                    # 检查API返回结果
                    if data.get("status") == "success" and data.get("code") == 200:
                        return data.get("data", "未知结果")
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
        logger.error(f"检查链接时出错: {str(e)}")
        return None

def setup_sf_handlers(application):
    """设置sf命令处理器"""
    application.add_handler(CommandHandler("sf", sf_command))
    logger.info("分享链接隐私检测命令 (/sf) 处理器已设置")
