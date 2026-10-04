"""凭据类命令的聊天上下文限制：只允许私聊。

适用命令见 ``core.redaction.PRIVATE_ONLY_COMMANDS``。在群聊等非私聊环境调用时
不执行命令、不读取参数，提示改用私聊，并尽力删除用户发出的那条消息。
"""

from __future__ import annotations

import functools
import logging

from telegram import Update
from telegram.ext import ContextTypes

from . import redaction
from .telegram_history import is_delegated_command

logger = logging.getLogger(__name__)


def _rejection_text(command: str, *, deletion_attempted: bool) -> str:
    deletion_note = (
        "已尝试删除你刚才发送的消息；如果仍然可见，请手动删除。\n"
        if deletion_attempted
        else "如果刚才的消息里包含卡密或密码，请手动删除它。\n"
    )
    return (
        f"⚠️ /{command} 涉及账户凭据，只能在与我的私聊中使用，请私聊我后再发送。\n"
        f"{deletion_note}\n"
        f"/{command} handles account credentials, so it only works in a private chat "
        "with me. Please message me privately."
    )


async def reject_outside_private_chat(update: Update, command: str) -> None:
    """拒绝群聊中的凭据类命令，并尽力删除含凭据的用户消息。"""
    message = update.effective_message
    if message is None:
        return

    # AI 代执行时 message_id 指向用户触发 AI 的原消息，不由工具替用户删除。
    deletion_attempted = not is_delegated_command()
    try:
        await message.reply_text(
            _rejection_text(command, deletion_attempted=deletion_attempted)
        )
    except Exception as exc:
        logger.warning(
            "发送私聊限制提示失败: command=/%s chat_id=%s (%s)",
            command,
            getattr(update.effective_chat, "id", None),
            redaction.describe_exception(exc),
        )

    if not deletion_attempted:
        return
    try:
        await message.delete()
    except Exception as exc:
        logger.warning(
            "无法删除含凭据的命令消息: command=/%s chat_id=%s message_id=%s (%s)",
            command,
            getattr(update.effective_chat, "id", None),
            getattr(message, "message_id", None),
            redaction.describe_exception(exc),
        )


def private_chat_only(command: str):
    """装饰命令 handler：非私聊环境只回复提示，不调用原 handler。"""

    def decorator(handler):
        @functools.wraps(handler)
        async def wrapper(
            update: Update,
            context: ContextTypes.DEFAULT_TYPE,
            *args,
            **kwargs,
        ):
            chat = update.effective_chat
            if chat is not None and chat.type != "private":
                await reject_outside_private_chat(update, command)
                return None
            return await handler(update, context, *args, **kwargs)

        return wrapper

    return decorator
