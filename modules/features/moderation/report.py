import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CommandHandler, CallbackQueryHandler
from telegram.constants import ParseMode
import time
from core.command_cooldown import cooldown

# 创建日志记录器
logger = logging.getLogger(__name__)

# 缓存举报消息，防止短时间内重复举报
# 格式: {message_id: {reported_at: timestamp, reported_by: [user_id1, user_id2, ...]}}
report_cache = {}
# 缓存过期时间（秒）
CACHE_EXPIRY = 3600  # 1小时


@cooldown
async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理 /report 命令，用于举报消息"""
    # 检查是否在群组中
    if update.effective_chat.type not in ["group", "supergroup"]:
        await update.message.reply_text("此命令只能在群组中使用。")
        return
    
    # 检查是否回复了消息
    if not update.message.reply_to_message:
        await update.message.reply_text("请回复您要举报的消息，并附带 /report 命令。")
        return
    
    # 获取被举报的消息信息
    reported_message = update.message.reply_to_message
    reported_message_id = reported_message.message_id
    reported_user = reported_message.from_user
    reported_user_id = reported_user.id
    reported_user_name = reported_user.full_name
    reported_user_username = reported_user.username
    reported_text = reported_message.text or reported_message.caption or "消息内容无法获取"
    
    # 获取举报人信息
    reporter_user = update.message.from_user
    reporter_user_id = reporter_user.id
    reporter_user_name = reporter_user.full_name
    reporter_user_username = reporter_user.username
    
    # 检查是否重复举报
    current_time = time.time()
    if reported_message_id in report_cache:
        report_info = report_cache[reported_message_id]
        # 检查缓存是否过期
        if current_time - report_info["reported_at"] < CACHE_EXPIRY:
            # 检查该用户是否已经举报过
            if reporter_user_id in report_info["reported_by"]:
                await update.message.reply_text("您已经举报过这条消息了。")
                return
            # 添加到已举报用户列表
            report_info["reported_by"].append(reporter_user_id)
        else:
            # 缓存已过期，重新创建
            report_cache[reported_message_id] = {
                "reported_at": current_time,
                "reported_by": [reporter_user_id]
            }
    else:
        # 创建新的举报记录
        report_cache[reported_message_id] = {
            "reported_at": current_time,
            "reported_by": [reporter_user_id]
        }
    
    # 获取管理员列表
    try:
        chat_administrators = await context.bot.get_chat_administrators(update.effective_chat.id)
        admin_count = len(chat_administrators)
    except Exception as e:
        logger.error(f"获取群组管理员时出错: {e}")
        await update.message.reply_text("举报处理过程中出错，请稍后再试。")
        return
    
    # 创建举报消息
    report_info = (
        f"*== 举报信息 ==*\n\n"
        f"*群组:* {update.effective_chat.title}\n"
        f"*群组ID:* `{update.effective_chat.id}`\n\n"
        f"*被举报用户:* {reported_user_name}"
    )
    
    if reported_user_username:
        report_info += f" (@{reported_user_username})"
    
    report_info += (
        f"\n*用户ID:* `{reported_user_id}`\n\n"
        f"*被举报消息:*\n{reported_text[:300]}{'...' if len(reported_text) > 300 else ''}\n\n"
        f"*举报人:* {reporter_user_name}"
    )
    
    if reporter_user_username:
        report_info += f" (@{reporter_user_username})"
    
    report_info += f"\n*举报人ID:* `{reporter_user_id}`"
    
    # 创建按钮，用于跳转到该消息
    message_link = f"https://t.me/c/{str(update.effective_chat.id)[4:]}/{reported_message_id}"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("查看被举报消息", url=message_link)]
    ])
    
    # 向所有管理员发送举报信息
    success_count = 0
    for admin in chat_administrators:
        try:
            await context.bot.send_message(
                chat_id=admin.user.id,
                text=report_info,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=keyboard
            )
            success_count += 1
        except Exception as e:
            logger.error(f"向管理员 {admin.user.id} 发送举报信息失败: {e}")
    
    # 回复举报者
    if success_count > 0:
        await update.message.reply_text(
            f"您的举报已发送给群组管理员({success_count}/{admin_count})。"
        )
    else:
        await update.message.reply_text(
            "无法发送举报信息给管理员，请直接联系群组管理员处理。"
        )


def setup_report_handlers(application):
    """注册处理函数"""
    application.add_handler(CommandHandler("report", report_command))
    logging.info("已加载举报功能处理器")
