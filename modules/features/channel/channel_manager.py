import json
import logging
from typing import Optional

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from core import mysql_connection
from core.command_cooldown import cooldown

logger = logging.getLogger(__name__)

MAX_CHANNEL_POSTS = 1000


def _parse_channel_ref(value: str) -> str | int:
    """解析频道参数，支持 @username 或频道数值 ID。"""
    raw = (value or "").strip()
    if not raw:
        return ""
    if raw.lstrip("-").isdigit():
        return int(raw)
    if not raw.startswith("@"):
        return f"@{raw}"
    return raw


def _extract_message_data(message) -> tuple[str, Optional[str], Optional[str], Optional[str]]:
    """提取频道消息的类型与文本/媒体信息。"""
    if message.text:
        return "text", message.text, None, None
    if message.photo:
        file_id = message.photo[-1].file_id if message.photo else None
        return "photo", None, message.caption, file_id
    if message.video:
        return "video", None, message.caption, message.video.file_id
    if message.document:
        return "document", None, message.caption, message.document.file_id
    if message.audio:
        return "audio", None, message.caption, message.audio.file_id
    if message.voice:
        return "voice", None, message.caption, message.voice.file_id
    if message.animation:
        return "animation", None, message.caption, message.animation.file_id
    if message.sticker:
        return "sticker", None, message.caption, message.sticker.file_id
    return "unknown", None, message.caption, None


@cooldown
async def channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理 /channel 指令，用于绑定或解绑频道。"""
    if not update.message:
        return
    if update.effective_chat and update.effective_chat.type != "private":
        await update.message.reply_text("请在私聊中使用 /channel 指令。")
        return

    args = context.args or []
    if not args:
        await update.message.reply_text("用法：/channel bind <频道> 或 /channel unbind")
        return

    action = (args[0] or "").strip().lower()
    user_id = update.effective_user.id

    if action == "bind":
        if len(args) < 2:
            await update.message.reply_text("请提供频道用户名或频道 ID，例如：/channel bind @your_channel")
            return

        channel_ref = _parse_channel_ref(args[1])
        if not channel_ref:
            await update.message.reply_text("频道参数无效，请重新输入。")
            return

        try:
            chat = await context.bot.get_chat(channel_ref)
        except TelegramError as exc:
            logger.warning("获取频道失败: %s", exc)
            await update.message.reply_text("无法获取频道信息，请确认频道用户名或 ID 是否正确。")
            return

        if not chat or chat.type != "channel":
            await update.message.reply_text("目标不是频道，请提供正确的频道用户名或 ID。")
            return

        try:
            bot_user = await context.bot.get_me()
            bot_member = await context.bot.get_chat_member(chat.id, bot_user.id)
            if bot_member.status not in {"administrator", "creator"}:
                await update.message.reply_text("请先将雾萌娘设为该频道管理员。")
                return

            user_member = await context.bot.get_chat_member(chat.id, user_id)
            if user_member.status not in {"administrator", "creator"}:
                await update.message.reply_text("绑定失败：你不是该频道的管理员或创建者。")
                return
        except TelegramError as exc:
            logger.warning("校验频道权限失败: %s", exc)
            await update.message.reply_text("无法验证频道权限，请确认机器人已是频道管理员。")
            return

        async with mysql_connection.transaction() as connection:
            conflict_row = await mysql_connection.fetch_one(
                "SELECT user_id FROM ai_user_channel_bindings WHERE channel_id = %s",
                (chat.id,),
                connection=connection,
            )
            if conflict_row and conflict_row[0] != user_id:
                await update.message.reply_text("该频道已被其他用户绑定，无法重复绑定。")
                return

            await connection.exec_driver_sql(
                "INSERT INTO ai_user_channel_bindings (user_id, channel_id, channel_title, channel_username) "
                "VALUES (%s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE channel_id = VALUES(channel_id), "
                "channel_title = VALUES(channel_title), "
                "channel_username = VALUES(channel_username), "
                "updated_at = CURRENT_TIMESTAMP",
                (user_id, chat.id, chat.title, chat.username),
            )

        channel_label = f"@{chat.username}" if chat.username else str(chat.id)
        await update.message.reply_text(f"绑定成功：{channel_label}")
        return

    if action == "unbind":
        row = await mysql_connection.fetch_one(
            "SELECT channel_id, channel_title, channel_username FROM ai_user_channel_bindings WHERE user_id = %s",
            (user_id,),
        )
        if not row:
            await update.message.reply_text("你还没有绑定任何频道。")
            return
        await mysql_connection.execute(
            "DELETE FROM ai_user_channel_bindings WHERE user_id = %s",
            (user_id,),
        )
        channel_id, channel_title, channel_username = row
        channel_label = f"@{channel_username}" if channel_username else str(channel_id)
        display_title = channel_title or "频道"
        await update.message.reply_text(f"已解绑：{display_title}（{channel_label}）")
        return

    if action in {"status", "show"}:
        row = await mysql_connection.fetch_one(
            "SELECT channel_id, channel_title, channel_username FROM ai_user_channel_bindings WHERE user_id = %s",
            (user_id,),
        )
        if not row:
            await update.message.reply_text("你还没有绑定任何频道。")
            return
        channel_id, channel_title, channel_username = row
        channel_label = f"@{channel_username}" if channel_username else str(channel_id)
        display_title = channel_title or "频道"
        await update.message.reply_text(f"当前绑定频道：{display_title}（{channel_label}）")
        return

    await update.message.reply_text("未知操作，请使用 /channel bind 或 /channel unbind")


async def channel_post_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """接收频道消息并写入数据库，用于后续读取。"""
    message = update.channel_post or update.edited_channel_post
    if not message:
        return

    channel_id = message.chat_id
    row = await mysql_connection.fetch_one(
        "SELECT 1 FROM ai_user_channel_bindings WHERE channel_id = %s",
        (channel_id,),
    )
    if not row:
        return

    message_type, text, caption, file_id = _extract_message_data(message)
    raw_json = json.dumps(message.to_dict(), ensure_ascii=False, default=str)

    async with mysql_connection.transaction() as connection:
        await connection.exec_driver_sql(
            "INSERT INTO ai_channel_posts (channel_id, message_id, message_date, message_type, text, caption, file_id, raw_json) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE message_type = VALUES(message_type), "
            "text = VALUES(text), caption = VALUES(caption), file_id = VALUES(file_id), raw_json = VALUES(raw_json)",
            (
                channel_id,
                message.message_id,
                message.date,
                message_type,
                text,
                caption,
                file_id,
                raw_json,
            ),
        )
        await connection.exec_driver_sql(
            "DELETE FROM ai_channel_posts "
            "WHERE channel_id = %s AND message_id NOT IN ("
            "  SELECT message_id FROM ("
            "    SELECT message_id FROM ai_channel_posts "
            "    WHERE channel_id = %s "
            "    ORDER BY message_id DESC "
            "    LIMIT %s"
            "  ) AS keep_rows"
            ")",
            (channel_id, channel_id, MAX_CHANNEL_POSTS),
        )
