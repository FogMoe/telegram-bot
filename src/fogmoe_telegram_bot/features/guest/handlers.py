"""Guest Mode 入口：bot 不在的聊天里，有人 @ 它或回复它的 guest 回复。

功能还没接进来，先回一段固定的自我介绍。guest 更新在所有 handler 之前截住：不进历史，
也不会被命令 handler 当成普通消息执行（PTB 把 `guest_message` 算进了 `effective_message`）。
"""

import logging
from typing import Any

from telegram import MessageEntity, Update
from telegram.ext import ApplicationHandlerStop, ContextTypes, MessageHandler, filters

from fogmoe_telegram_bot.core.guest_replies import GuestReplySession, guest_reply_scope
from fogmoe_telegram_bot.core.redaction import log_exception
from fogmoe_telegram_bot.core.telegram_history import suppress_telegram_history

logger = logging.getLogger(__name__)

# 早于历史入口（-100）：guest 更新不经过任何别的 handler。
GUEST_HANDLER_GROUP = -110


def intro_text(username: str | None) -> str:
    where = f"私聊 @{username}" if username else "私聊我"
    where_en = f"DM @{username}" if username else "DM me"
    return (
        "你好喵！我是雾萌娘（FogMoeBot），可以陪你聊天，还有签到、小游戏、行情查询和群管理这些功能。\n"
        f"被临时叫出来的时候只能先打个招呼，想用全部功能请{where}，或者把我拉进群里～\n\n"
        "Hi! I'm FogMoeBot: a chat companion with check-ins, mini games, crypto prices and group tools. "
        f"When called in as a guest I can only say hello for now. {where_en} or add me to your group "
        "for everything else."
    )


def _bot_username(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    try:
        return context.bot.username
    except RuntimeError:
        return None


async def handle_guest_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.guest_message
    if message is not None:
        entities = message.entities or ()
        # 只记标识和形态，不记文本：用来确认哪些写法会触发 guest 消息。
        logger.info(
            "收到 guest 消息: update_id=%s chat_id=%s chat_type=%s user_id=%s command=%s reply=%s",
            update.update_id,
            message.chat.id,
            message.chat.type,
            getattr(message.from_user, "id", None),
            any(entity.type == MessageEntity.BOT_COMMAND for entity in entities),
            message.reply_to_message is not None,
        )
        try:
            with (
                guest_reply_scope(GuestReplySession.from_message(message)),
                suppress_telegram_history(),
            ):
                await message.reply_text(intro_text(_bot_username(context)))
        except Exception as exc:
            log_exception(
                logger,
                f"guest 回复失败: update_id={update.update_id} chat_id={message.chat.id}",
                exc,
            )
    raise ApplicationHandlerStop


def setup_guest_handlers(application: Any) -> None:
    application.add_handler(
        MessageHandler(filters.UpdateType.GUEST_MESSAGE, handle_guest_message),
        group=GUEST_HANDLER_GROUP,
    )
