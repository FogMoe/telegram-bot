"""`/keyword` 与群消息的关键词自动回复：Telegram 适配层。

解析命令、检查管理员、把结果映射成回复；数量上限、HTML 净化、匹配、触发频率与缓存在 `operations/keyword_replies.py`。
"""

import html
import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import CommandHandler, ContextTypes, MessageHandler, filters

from fogmoe_telegram_bot.core.command_cooldown import cooldown
from fogmoe_telegram_bot.core.redaction import report_error
from fogmoe_telegram_bot.features.moderation.operations import keyword_replies
from fogmoe_telegram_bot.features.moderation.operations.keyword_replies import AddReplyStatus

logger = logging.getLogger(__name__)

USAGE_HINT = "使用 /keyword add <触发关键词> <回复内容> 添加关键词\n使用 /keyword del <触发关键词> 删除关键词"

_ADD_REPLY_TEXT = {
    AddReplyStatus.KEYWORD_TOO_LONG: "触发关键词太长，请不要超过255个字符。",
    AddReplyStatus.RESPONSE_TOO_LONG: f"回复内容太长，请不要超过{keyword_replies.MAX_RESPONSE_LENGTH}个字符。",
    AddReplyStatus.LIMIT_REACHED: "每个群组最多只能设置10个关键词，请先删除一些关键词再添加。",
    AddReplyStatus.UPDATED: "已更新关键词触发器：'{keyword}'",
    AddReplyStatus.ADDED: "已添加关键词触发器：'{keyword}'",
}


@cooldown
async def keyword_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理/keyword命令，显示、添加或删除关键词"""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if update.effective_chat.type not in ["group", "supergroup"]:
        await update.message.reply_text("此命令只能在群组中使用。\nThis command can only be used in groups.")
        return

    chat_member = await context.bot.get_chat_member(chat_id, user_id)
    if chat_member.status not in ["administrator", "creator"]:
        await update.message.reply_text("只有群组管理员才能使用此命令。\nOnly group administrators can use this command.")
        return

    if not context.args:
        await show_keywords(update, chat_id)
        return

    sub_command = context.args[0].lower()

    if sub_command == "add":
        if len(context.args) < 3:
            await update.message.reply_text(
                "添加关键词的正确格式是：\n/keyword add <触发关键词> <回复内容>\n\n"
                "回复内容支持 Telegram HTML 格式。"
            )
            return
        await add_keyword(update, chat_id, user_id, context.args[1], " ".join(context.args[2:]))

    elif sub_command == "del":
        if len(context.args) < 2:
            await update.message.reply_text("删除关键词的正确格式是：\n/keyword del <触发关键词>")
            return
        await del_keyword(update, chat_id, context.args[1])

    else:
        await update.message.reply_text(
            "未知的子命令。可用的命令有：\n"
            "/keyword - 显示关键词列表\n"
            "/keyword add <触发关键词> <回复内容> - 添加关键词\n"
            "/keyword del <触发关键词> - 删除关键词"
        )


async def show_keywords(update: Update, chat_id: int):
    try:
        replies = await keyword_replies.get_replies(chat_id)

        if not replies:
            await update.message.reply_text(
                f"此群组尚未设置任何关键词。\n\n{USAGE_HINT}\n\n回复内容支持 Telegram HTML 格式。"
            )
            return

        message = "当前群组的关键词列表：\n\n"
        for idx, reply in enumerate(replies, 1):
            message += f"{idx}. 触发词: '{reply.keyword}'\n"
            if len(reply.response) > 30:
                message += f"   回复: '{reply.response[:30]}...'\n\n"
            else:
                message += f"   回复: '{reply.response}'\n\n"
        message += f"\n{USAGE_HINT}"

        await update.message.reply_text(message)
    except Exception as e:
        notice = report_error(logger, "获取关键词列表时出错", e)
        await update.message.reply_text(f"获取关键词列表时出错，请稍后再试。\n{notice}")


async def add_keyword(update: Update, chat_id: int, user_id: int, keyword: str, response: str):
    try:
        status = await keyword_replies.add_reply(chat_id, user_id, keyword, response)
        await update.message.reply_text(_ADD_REPLY_TEXT[status].format(keyword=keyword))
    except Exception as e:
        notice = report_error(logger, "添加关键词时出错", e)
        await update.message.reply_text(f"添加关键词时出错，请稍后再试。\n{notice}")


async def del_keyword(update: Update, chat_id: int, keyword: str):
    try:
        if await keyword_replies.delete_reply(chat_id, keyword):
            await update.message.reply_text(f"已删除关键词触发器：'{keyword}'")
        else:
            await update.message.reply_text(f"未找到关键词：'{keyword}'")
    except Exception as e:
        notice = report_error(logger, "删除关键词时出错", e)
        await update.message.reply_text(f"删除关键词时出错，请稍后再试。\n{notice}")


async def process_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """群消息命中关键词时回复。"""
    effective_message = update.message or update.edited_message

    if (update.effective_chat.type not in ["group", "supergroup"] or
        not effective_message or
        not effective_message.text or
        effective_message.text.startswith('/')):
        return

    reply = await keyword_replies.find_reply(update.effective_chat.id, effective_message.text)
    if reply is None:
        return
    try:
        await effective_message.reply_text(
            keyword_replies.sanitize_html(reply.response), parse_mode=ParseMode.HTML
        )
    except Exception as e:
        # HTML 解析失败时转义后按纯文本发送
        logger.warning(f"HTML解析失败，尝试纯文本: {str(e)}")
        await effective_message.reply_text(f"【回复内容HTML格式错误】\n\n{html.escape(reply.response)}")


def setup_keyword_handlers(dispatcher):
    dispatcher.add_handler(CommandHandler("keyword", keyword_command))
    # 优先级低于垃圾信息过滤（group 5）；编辑后的消息也检查
    dispatcher.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS &
            (filters.UpdateType.MESSAGE | filters.UpdateType.EDITED_MESSAGE),
            process_group_message
        ),
        group=10,
    )
