import logging

from telegram import Update
from telegram.ext import ContextTypes

from core.telegram_history import telegram_history_scope


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理Telegram API错误"""
    logging.error(f"Update {update} caused error {context.error}")

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
                    "错误信息 Error message: \n\n" + str(context.error) + "\n\n您可以发送给管理员 @ScarletKc 报告此问题。\n"
                    "You can report this issue to the admin @ScarletKc."
                )
            elif update and update.callback_query:
                # 对回调查询错误的处理
                await update.callback_query.answer("处理请求时出错，请稍后再试")
                if update.effective_chat:
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text="操作出错，请稍后再试。\n错误信息: " + str(context.error)
                    )
    except Exception as e:
        logging.error(f"在处理错误时又发生了错误: {str(e)}")
