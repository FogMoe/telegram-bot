import logging
import time

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import CommandHandler, ContextTypes
from sqlalchemy.exc import SQLAlchemyError

from core import mysql_connection
from core.command_cooldown import cooldown  # 导入命令冷却装饰器

# 创建日志记录器
logger = logging.getLogger(__name__)

# 创建缓存
token_cache = {}  # 群组ID -> (chain, ca) 的映射
cache_timestamps = {}  # 群组ID -> 缓存时间戳的映射
CACHE_EXPIRY = 600  # 缓存过期时间（秒），10分钟


async def bind_token_for_group(group_id, chain, ca, set_by):
    try:
        async with mysql_connection.transaction() as connection:
            row = await mysql_connection.fetch_one(
                "SELECT 1 FROM group_chart_tokens WHERE group_id = %s",
                (group_id,),
                connection=connection,
            )
            if row:
                await connection.exec_driver_sql(
                    "UPDATE group_chart_tokens SET chain = %s, ca = %s, set_by = %s WHERE group_id = %s",
                    (chain, ca, set_by, group_id),
                )
            else:
                await connection.exec_driver_sql(
                    "INSERT INTO group_chart_tokens (group_id, chain, ca, set_by) VALUES (%s, %s, %s, %s)",
                    (group_id, chain, ca, set_by),
                )

        token_cache[group_id] = (chain, ca)
        cache_timestamps[group_id] = time.time()
        return True
    except SQLAlchemyError as e:
        logger.error(f"数据库错误: {str(e)}")
        return False


async def get_group_token(group_id):
    current_time = time.time()
    if group_id in token_cache and group_id in cache_timestamps:
        if current_time - cache_timestamps[group_id] < CACHE_EXPIRY:
            logger.info(f"从缓存获取群组 {group_id} 的代币信息")
            return token_cache[group_id]

    row = await mysql_connection.fetch_one(
        "SELECT chain, ca FROM group_chart_tokens WHERE group_id = %s",
        (group_id,),
    )
    if row:
        token_cache[group_id] = (row[0], row[1])
        cache_timestamps[group_id] = current_time
        return token_cache[group_id]
    return None


def clean_expired_cache():
    current_time = time.time()
    expired_keys = [
        group_id for group_id in cache_timestamps
        if current_time - cache_timestamps[group_id] >= CACHE_EXPIRY
    ]

    for group_id in expired_keys:
        token_cache.pop(group_id, None)
        cache_timestamps.pop(group_id, None)

    if expired_keys:
        logger.info(f"已清理 {len(expired_keys)} 个过期缓存条目")


async def is_user_admin(update: Update):
    user_id = update.effective_user.id
    try:
        chat_member = await update.effective_chat.get_member(user_id)
        return chat_member.status in ["creator", "administrator"]
    except Exception as e:
        logger.error(f"检查管理员权限时出错: {str(e)}")
        return False


async def delete_token_for_group(group_id):
    try:
        await mysql_connection.execute(
            "DELETE FROM group_chart_tokens WHERE group_id = %s",
            (group_id,),
        )
        token_cache.pop(group_id, None)
        cache_timestamps.pop(group_id, None)
        return True
    except SQLAlchemyError as e:
        logger.error(f"数据库错误: {str(e)}")
        return False


@cooldown
async def chart_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ["group", "supergroup"]:
        await update.message.reply_text("此命令只能在群组中使用。")
        return

    args = context.args

    if len(args) >= 3 and args[0].lower() == "bind":
        if not await is_user_admin(update):
            await update.message.reply_text("只有群组管理员才能绑定代币。")
            return

        chain = args[1].lower()
        ca = args[2]

        valid_chains = ["sol", "solana", "eth", "ethereum", "blast", "bsc", "bnb"]
        if chain not in valid_chains:
            await update.message.reply_text(f"不支持的链名称。支持的链: {', '.join(valid_chains)}")
            return

        if chain in ["sol", "solana"]:
            chain = "sol"
        elif chain in ["eth", "ethereum"]:
            chain = "eth"
        elif chain in ["bsc", "bnb"]:
            chain = "bsc"

        user_id = update.effective_user.id
        group_id = update.effective_chat.id

        success = await bind_token_for_group(group_id, chain, ca, user_id)

        if success:
            await update.message.reply_text(f"成功为群组绑定{chain}链上的代币。\n合约地址: {ca}")
        else:
            await update.message.reply_text("绑定代币失败，请稍后重试。")
        return

    if len(args) == 1 and args[0].lower() == "clear":
        if not await is_user_admin(update):
            await update.message.reply_text("只有群组管理员才能清除代币绑定。")
            return

        group_id = update.effective_chat.id
        success = await delete_token_for_group(group_id)

        if success:
            await update.message.reply_text("成功清除了群组的代币绑定。")
        else:
            await update.message.reply_text("清除代币绑定失败，请稍后重试。")
        return

    if len(args) == 0:
        group_id = update.effective_chat.id
        if cache_timestamps:
            clean_expired_cache()
        token_info = await get_group_token(group_id)

        if token_info:
            chain, ca = token_info
            chart_url = f"https://www.gmgn.cc/kline/{chain}/{ca}"

            chain_display_names = {
                "sol": "Solana",
                "eth": "Ethereum",
                "blast": "Blast",
                "bsc": "BSC",
            }
            chain_display = chain_display_names.get(chain, chain.upper())

            await update.message.reply_text(
                f"🔍 *代币图表*\n\n"
                f"链: {chain_display}\n"
                f"合约: `{ca}`\n\n"
                f"[点击查看图表]({chart_url})",
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=False,
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
