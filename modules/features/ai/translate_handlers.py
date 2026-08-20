import logging
import time
from uuid import uuid4

from telegram import InlineQueryResultArticle, InputTextMessageContent, Update
from telegram.constants import ParseMode
from telegram.ext import CommandHandler, ContextTypes

from core import mysql_connection, process_user, stake_reward_pool
from core.command_cooldown import cooldown
from features.ai import ai_chat

logger = logging.getLogger(__name__)


async def inline_translate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.inline_query.query
    user_id = update.effective_user.id
    now = time.time()

    # 从 context.user_data 获取用户注册状态和上次检查时间
    user_registered = context.user_data.get("is_registered", None)
    last_check_time = context.user_data.get("last_check_time", 0)

    # 如果缓存过期(1小时)或未检查过，则查询数据库
    if user_registered is None or (now - last_check_time > 3600):
        user_registered = await mysql_connection.async_check_user_exists(user_id)
        context.user_data["is_registered"] = user_registered
        context.user_data["last_check_time"] = now

    # 检查用户是否已注册
    if not user_registered:
        results = [
            InlineQueryResultArticle(
                id=str(uuid4()),
                title="请先获取个人信息 Please Register First",
                description="使用 /me 命令后即可使用翻译功能。 Using the /me command first to translate.",
                input_message_content=InputTextMessageContent(
                    message_text=f"{query}",
                    parse_mode=ParseMode.MARKDOWN
                )
            )
        ]
        await update.inline_query.answer(results, cache_time=300)
        return

    # 简单的长度判断，太短就跳过
    if not query or len(query) < 2:
        return

    now = time.time()
    last_query_time = context.user_data.get("last_query_time", 0)

    # 若距离上次query不足 2秒，跳过实际翻译，返回提示
    if now - last_query_time < 2:
        results = [
            InlineQueryResultArticle(
                id=str(uuid4()),
                title="请继续输入... Please continue typing...",
                description="停止输入2秒后进行翻译。 Stop typing for 2 seconds before translating.",
                input_message_content=InputTextMessageContent(
                    message_text=f"{query}",
                    parse_mode=ParseMode.MARKDOWN
                )
            )
        ]
        await update.inline_query.answer(results, cache_time=0)
        return

    context.user_data["last_query_time"] = now

    try:
        # 调用异步翻译函数
        translation = await ai_chat.translate_text(query)

        results = [
            InlineQueryResultArticle(
                id=str(uuid4()),
                title="发送翻译结果 Send Translation",
                description=translation[:100] + "..." if len(translation) > 100 else translation,
                input_message_content=InputTextMessageContent(
                    message_text=f"{translation}",
                    parse_mode=ParseMode.MARKDOWN
                )
            )
        ]
        await update.inline_query.answer(results, cache_time=10)

    except Exception as e:
        logging.error(f"内联翻译出错: {str(e)}")
        results = [
            InlineQueryResultArticle(
                id=str(uuid4()),
                title="翻译出错 Translation Error",
                description="翻译服务暂时不可用，请稍后重试 Translation service is temporarily unavailable, please try again later",
                input_message_content=InputTextMessageContent(
                    message_text=f"{query}",
                    parse_mode=ParseMode.MARKDOWN
                )
            )
        ]
        await update.inline_query.answer(results, cache_time=0)


@cooldown
async def tl_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """翻译命令处理函数"""
    # 获取用户ID以检查是否已注册
    user_id = update.effective_user.id
    if not await mysql_connection.async_check_user_exists(user_id):
        await update.message.reply_text(
            "请先使用 /me 命令注册个人信息后再使用翻译功能。\n"
            "Please register first using the /me command before using translation."
        )
        return

    text_to_translate = ""

    # 检查是否有回复消息
    if update.message.reply_to_message and update.message.reply_to_message.text:
        text_to_translate = update.message.reply_to_message.text
    # 检查是否有命令参数
    elif context.args:
        text_to_translate = " ".join(context.args)
    # 如果都没有，提示用法
    else:
        await update.message.reply_text(
            "使用方法：\n"
            "1. 回复一条消息并使用 /tl 命令\n"
            "2. 直接使用 /tl <文本> 进行翻译\n\n"
            "Usage:\n"
            "1. Reply to a message with /tl command\n"
            "2. Use /tl <text> to translate directly"
        )
        return

    # 如果文本过长，拒绝翻译
    if len(text_to_translate) > 3000:
        await update.message.reply_text(
            "文本太长，无法翻译。请尝试缩短文本。\n"
            "Text too long for translation. Please try with a shorter text."
        )
        return

    # 检查硬币是否足够（基于长度收费）
    coin_cost = 0
    if len(text_to_translate) > 500:
        coin_cost = 1
    if len(text_to_translate) > 1000:
        coin_cost = 2
    if len(text_to_translate) > 2000:
        coin_cost = 3

    # 获取用户硬币数
    user_coins = await process_user.async_get_user_coins(user_id)
    if user_coins < coin_cost:
        await update.message.reply_text(
            f"您的硬币不足，需要 {coin_cost} 枚硬币进行翻译。试试通过 /lottery 抽奖获取硬币吧！\n"
            f"You don't have enough coins (need {coin_cost}). Try using /lottery to get some coins!"
        )
        return

    spent = await process_user.spend_user_coins(user_id, coin_cost)
    if not spent:
        await update.message.reply_text(
            f"您的硬币不足，需要 {coin_cost} 枚硬币进行翻译。试试通过 /lottery 抽奖获取硬币吧！\n"
            f"You don't have enough coins (need {coin_cost}). Try using /lottery to get some coins!"
        )
        return

    # 不发送正在翻译状态
    # await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    # 调用翻译函数
    try:
        translation = await ai_chat.translate_text(text_to_translate)
        await update.message.reply_text(
            f"{translation}"
        )
        try:
            pool_add = stake_reward_pool.calculate_pool_add(coin_cost)
            if pool_add > 0:
                await stake_reward_pool.add_to_pool(pool_add)
        except Exception as pool_error:
            logger.error("更新奖励池失败: %s", pool_error)
    except Exception as e:
        logging.error(f"翻译出错: {str(e)}")
        await update.message.reply_text(
            "翻译服务暂时不可用，请稍后重试。\n"
            "Translation service is temporarily unavailable, please try again later. Your coins have been refunded."
        )
        await process_user.add_free_coins(user_id, coin_cost)


def setup_translation_handlers(application) -> None:
    """注册翻译命令。内联翻译暂时禁用，见 inline_translate。"""

    application.add_handler(CommandHandler("tl", tl_command))
