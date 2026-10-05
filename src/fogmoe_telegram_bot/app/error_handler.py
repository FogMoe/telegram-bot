import logging

from telegram import Update
from telegram.constants import UpdateType
from telegram.ext import ContextTypes

from fogmoe_telegram_bot.core.redaction import log_exception, new_error_ref, user_error_notice
from fogmoe_telegram_bot.core.telegram_history import telegram_history_scope

logger = logging.getLogger(__name__)


def _describe_update(update: object) -> str:
    """只取排查需要的标识字段，不记录完整 Update（含消息正文与用户资料）。"""
    if update is None:
        return "update=None"
    if not isinstance(update, Update):
        return f"update_type={type(update).__name__}"

    update_kind = next(
        (
            kind.value
            for kind in UpdateType
            if getattr(update, kind.value, None) is not None
        ),
        "unknown",
    )
    chat = update.effective_chat
    user = update.effective_user
    return (
        f"update_id={update.update_id} update_kind={update_kind} "
        f"chat_id={getattr(chat, 'id', None)} chat_type={getattr(chat, 'type', None)} "
        f"user_id={getattr(user, 'id', None)}"
    )


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理Telegram API错误"""
    error_ref = new_error_ref()
    log_exception(
        logger,
        f"Unhandled error ({_describe_update(update)})",
        context.error,
        ref=error_ref,
    )

    # 根据不同类型的更新选择不同的回复方式
    try:
        with telegram_history_scope(
            origin="bot_runtime",
            event="error_notice",
            cause="telegram_update_failed",
        ):
            if update and update.effective_message:
                await update.effective_message.reply_text(
                    "看起来对话出现了一些小问题呢。"
                    "您可以尝试使用 /clear 命令来清空聊天记录，"
                    "然后我们重新开始对话吧！\n"
                    "It seems there was a small issue with the conversation."
                    "You can try using the  /clear  command to clear the chat history,"
                    "and then we can start over!\n\n"
                    f"{user_error_notice(error_ref)}\n\n"
                    "您可以把这个参考 ID 发送给管理员 @ScarletKc 报告此问题。\n"
                    "You can report this issue to the admin @ScarletKc with this reference."
                )
            elif update and update.callback_query:
                # 对回调查询错误的处理
                await update.callback_query.answer("处理请求时出错，请稍后再试")
                if update.effective_chat:
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text=f"操作出错，请稍后再试。\n{user_error_notice(error_ref)}",
                    )
    except Exception as e:
        log_exception(logger, "在处理错误时又发生了错误", e, ref=error_ref)
