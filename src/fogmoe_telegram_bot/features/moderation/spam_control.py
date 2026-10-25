"""`/spam` 与群消息的垃圾信息过滤：Telegram 适配层。

解析命令、检查管理员与机器人权限、删除消息并发出警告；开关、垃圾词与警告计数的规则在
`operations/spam_control.py`，文案在 `spam_control_views.py`，AI 识别在 `spam_ai/`。
"""

import logging
import threading
import time

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from fogmoe_telegram_bot.core.command_cooldown import cooldown
from fogmoe_telegram_bot.core.redaction import report_error
from fogmoe_telegram_bot.features.moderation import spam_control_views as views
from fogmoe_telegram_bot.features.moderation import spam_strikes
from fogmoe_telegram_bot.features.moderation.operations import spam_control as spam_rules
from fogmoe_telegram_bot.features.moderation.spam_ai import handlers as spam_ai_handlers
from fogmoe_telegram_bot.features.moderation.spam_ai import operations as spam_ai_operations

logger = logging.getLogger(__name__)

ADMIN_STATUSES = ("administrator", "creator")
# 发消息的人是不是管理员，在 chat_data 里缓存这么久
ADMIN_CACHE_SECONDS = 300
# 「查看更多功能」按钮的防抖：{"用户:群:spam_help": 上次点击时刻}
CALLBACK_COOLDOWN_SECONDS = 3
_callback_clicks: dict[str, float] = {}


@cooldown
async def toggle_spam_control(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /spam 命令"""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if update.effective_chat.type not in ["group", "supergroup"]:
        await update.message.reply_text("此命令只能在群组中使用。")
        return

    try:
        chat_member = await context.bot.get_chat_member(chat_id, user_id)
        if chat_member.status not in ADMIN_STATUSES:
            await update.message.reply_text("只有群组管理员才能使用此命令。")
            return
    except Exception as e:
        logger.error(f"检查用户权限时出错: {str(e)}")
        await update.message.reply_text("检查权限时出错，请稍后再试。")
        return

    if context.args:
        sub_command = context.args[0].lower()

        if sub_command in ("links", "mentions"):
            await _blocking_command(update, context, sub_command, context.args[1:])
            return
        if sub_command == "add":
            if len(context.args) < 2:
                await update.message.reply_text(views.ADD_USAGE)
                return
            await _add_keyword(update, chat_id, user_id, " ".join(context.args[1:]))
            return
        if sub_command == "del":
            if len(context.args) < 2:
                await update.message.reply_text(views.DEL_USAGE)
                return
            await _delete_keyword(update, chat_id, " ".join(context.args[1:]))
            return
        if sub_command == "list":
            await _list_keywords(update, chat_id)
            return
        if sub_command == "help":
            await show_spam_control_help(update)
            return
        if sub_command == "ai":
            await spam_ai_handlers.spam_ai_command(
                update,
                context,
                context.args[1:],
                filter_enabled=await spam_rules.is_enabled(chat_id),
            )
            return

    # 没有子命令或子命令不认识：切换总开关
    try:
        bot_member = await context.bot.get_chat_member(chat_id, context.bot.id)
        if not bot_member.can_delete_messages:
            await update.message.reply_text(
                "机器人需要有删除消息的权限才能启用垃圾信息过滤功能。请先授予机器人管理员权限并允许其删除消息。"
            )
            return
    except Exception as e:
        logger.error(f"检查机器人权限时出错: {str(e)}")
        await update.message.reply_text("检查机器人权限时出错，请稍后再试。")
        return

    new_status = not await spam_rules.is_enabled(chat_id)
    try:
        await spam_rules.set_enabled(chat_id, user_id, new_status)
        if new_status:
            await update.message.reply_text(
                views.ENABLED_TEXT,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=views.help_keyboard(),
            )
        else:
            await update.message.reply_text(views.DISABLED_TEXT, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        notice = report_error(logger, "更新垃圾信息过滤状态时出错", e)
        await update.message.reply_text(f"操作失败，请稍后再试。\n{notice}")


async def _blocking_command(update: Update, context: ContextTypes.DEFAULT_TYPE, sub_command, args):
    """/spam links|mentions on|off"""
    if sub_command == "links":
        name, usage = "链接过滤", views.LINKS_USAGE
        apply, result_text = spam_rules.set_link_blocking, views.link_blocking_text
    else:
        name, usage = "@mention过滤", views.MENTIONS_USAGE
        apply, result_text = spam_rules.set_mention_blocking, views.mention_blocking_text
    if not args:
        await update.message.reply_text(usage)
        return
    option = args[0].lower()
    if option not in ("on", "off"):
        await update.message.reply_text(views.BAD_OPTION)
        return
    enable = option == "on"
    chat_id = update.effective_chat.id

    # 总开关打开之后才能设置
    if not await spam_rules.is_enabled(chat_id):
        await update.message.reply_text(f"请先开启垃圾信息过滤功能（使用 /spam 命令），才能设置{name}功能。")
        return

    try:
        bot_member = await context.bot.get_chat_member(chat_id, context.bot.id)
        if not bot_member.can_delete_messages:
            await update.message.reply_text(f"机器人需要有删除消息的权限才能使用{name}功能。")
            return
    except Exception as e:
        logger.error(f"检查机器人权限时出错: {str(e)}")
        await update.message.reply_text("检查机器人权限时出错，请稍后再试。")
        return

    try:
        await apply(chat_id, enable)
        await update.message.reply_text(result_text(enable))
    except Exception as e:
        notice = report_error(logger, f"更新{name}状态时出错", e)
        await update.message.reply_text(f"操作失败，请稍后再试。\n{notice}")


async def _add_keyword(update: Update, chat_id: int, user_id: int, keyword: str):
    try:
        result = await spam_rules.add_custom_keyword(chat_id, user_id, keyword)
        await update.message.reply_text(views.add_keyword_text(result))
    except Exception as e:
        notice = report_error(logger, "添加自定义垃圾词时出错", e)
        await update.message.reply_text(f"添加自定义垃圾词时出错，请稍后再试。\n{notice}")


async def _delete_keyword(update: Update, chat_id: int, keyword: str):
    try:
        result = await spam_rules.delete_custom_keyword(chat_id, keyword)
        await update.message.reply_text(views.delete_keyword_text(result))
    except Exception as e:
        notice = report_error(logger, "删除自定义垃圾词时出错", e)
        await update.message.reply_text(f"删除自定义垃圾词时出错，请稍后再试。\n{notice}")


async def _list_keywords(update: Update, chat_id: int):
    try:
        keywords = await spam_rules.list_custom_keywords(chat_id)
        await update.message.reply_text(views.keyword_list_text(keywords))
    except Exception as e:
        notice = report_error(logger, "获取自定义垃圾词列表时出错", e)
        await update.message.reply_text(f"获取自定义垃圾词列表时出错，请稍后再试。\n{notice}")


async def show_spam_control_help(update: Update):
    try:
        await update.message.reply_text(views.HELP_TEXT, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"发送帮助信息时出错: {str(e)}")
        await update.message.reply_text(views.HELP_TEXT_PLAIN)


def _too_soon(key: str, now: float) -> bool:
    """按钮防抖：冷却期内的点击返回 True；否则记下这次点击，并顺带清掉过期的记录。"""
    if now - _callback_clicks.get(key, 0) < CALLBACK_COOLDOWN_SECONDS:
        return True
    _callback_clicks[key] = now
    for other in list(_callback_clicks):
        if now - _callback_clicks[other] > CALLBACK_COOLDOWN_SECONDS * 2:
            _callback_clicks.pop(other, None)
    return False


async def spam_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """「查看更多功能」按钮：只有管理员能看。"""
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = update.effective_chat.id

    if _too_soon(f"{user_id}:{chat_id}:spam_help", time.time()):
        await query.answer("请不要频繁点击按钮", show_alert=True)
        return

    try:
        chat_member = await context.bot.get_chat_member(chat_id, user_id)
        if chat_member.status not in ADMIN_STATUSES:
            await query.answer("只有管理员可以查看此功能", show_alert=True)
            return
    except Exception as e:
        logger.error(f"验证用户权限时出错: {str(e)}")
        await query.answer("验证权限时出错，请稍后再试", show_alert=True)
        return

    await query.answer("正在加载帮助信息...")

    try:
        await query.edit_message_text(text=views.HELP_TEXT, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"编辑消息时出错: {str(e)}")
        # 编辑失败时发一条新消息，HTML 也失败就发纯文本
        try:
            await context.bot.send_message(chat_id=chat_id, text=views.HELP_TEXT, parse_mode=ParseMode.HTML)
        except Exception as send_error:
            logger.error(f"发送帮助消息时出错: {str(send_error)}")
            await context.bot.send_message(chat_id=chat_id, text=views.HELP_TEXT_PLAIN)


async def _sender_is_admin(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    """发消息的人是不是管理员，结果在 chat_data 里缓存 `ADMIN_CACHE_SECONDS` 秒。"""
    key = f"is_admin:{chat_id}:{user_id}"
    is_admin = context.chat_data.get(key, None)
    if is_admin is not None and time.time() <= context.chat_data.get(f"{key}_expire", 0):
        return is_admin
    try:
        chat_member = await context.bot.get_chat_member(chat_id, user_id)
    except Exception as e:
        if is_admin is None:
            logger.error(f"获取用户权限时出错: {e}")
            return False  # 查不到时按普通成员处理
        logger.error(f"刷新用户权限缓存时出错: {e}")
        return is_admin  # 刷新失败时沿用过期的结果
    is_admin = chat_member.status in ADMIN_STATUSES
    context.chat_data[key] = is_admin
    context.chat_data[f"{key}_expire"] = time.time() + ADMIN_CACHE_SECONDS
    return is_admin


async def process_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """群消息的过滤：链接、@提及、垃圾词依次检查，都没命中时交给 AI 识别。"""
    effective_message = update.message or update.edited_message

    # 文本消息检查正文，图片、视频等检查说明文字
    if not effective_message or not (effective_message.text or effective_message.caption):
        return
    if update.effective_chat.type not in ["group", "supergroup"]:
        return

    chat_id = update.effective_chat.id
    message_text = effective_message.text or effective_message.caption
    if len(message_text) < 2:
        return

    settings = await spam_rules.get_settings(chat_id)
    if not settings.enabled:
        return

    user_id = effective_message.from_user.id
    if await _sender_is_admin(context, chat_id, user_id):
        return

    if settings.block_links:
        found_url = spam_rules.find_link(message_text)
        if found_url is None:
            # 文字链接（「点击这里」背后的网址）不出现在正文里
            links = spam_ai_handlers.hidden_links(effective_message)
            found_url = links[0] if links else None
        if found_url is not None:
            user_mention = effective_message.from_user.mention_html()
            warning_count = spam_rules.add_warning(chat_id, user_id)
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=effective_message.message_id)
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=views.link_warning(user_mention, found_url, warning_count),
                    parse_mode=ParseMode.HTML,
                )
                logger.info(f"已删除链接消息 - 群组: {chat_id}, 用户: {user_id}, 链接: {found_url}, 内容: {message_text[:50]}...")
                return
            except Exception as e:
                logger.error(f"处理链接消息时出错: {e}")

    if settings.block_mentions:
        found_mention = spam_rules.find_mention(message_text)
        if found_mention is not None:
            user_mention = effective_message.from_user.mention_html()
            warning_count = spam_rules.add_warning(chat_id, user_id)
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=effective_message.message_id)
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=views.mention_warning(user_mention, found_mention, warning_count),
                    parse_mode=ParseMode.HTML,
                )
                logger.info(f"已删除@mention消息 - 群组: {chat_id}, 用户: {user_id}, 提及: {found_mention}, 内容: {message_text[:50]}...")
                return
            except Exception as e:
                logger.error(f"处理@mention消息时出错: {e}")

    trigger_word = await spam_rules.find_spam_word(message_text, chat_id)
    if trigger_word is not None:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=effective_message.message_id)
        except Exception as e:
            logger.error(f"删除垃圾消息时出错: {e}")
            return

        logger.info(f"已删除垃圾消息 - 群组: {chat_id}, 用户: {user_id}, 触发词: {trigger_word}, 内容: {message_text[:50]}...")
        reason_html = views.spam_reason_html(trigger_word)
        if await spam_rules.has_custom_keywords(chat_id):
            # 管理员自己设的垃圾词：和 AI 识别共用计数，满次数移出群组
            await spam_strikes.penalize(context.bot, effective_message, reason_html=reason_html)
            return

        # 全局词库按子串匹配，收录了不少常用词，命中只警告，不计入移出次数
        warning_count = spam_rules.add_warning(chat_id, user_id)
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=views.global_word_warning(
                    spam_strikes.sender_html(effective_message), reason_html, warning_count
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.error(f"发送垃圾信息警告时出错: {e}")
        return

    # 关键词没有命中：开了 AI 识别的群再交给 AI 检查
    await spam_ai_handlers.maybe_review(effective_message, context.bot)


def _schedule_warning_reset() -> None:
    """每 `WARNING_RESET_SECONDS` 秒清零一次警告计数（守护线程上的定时器）。"""

    def reset() -> None:
        spam_rules.reset_warnings()
        _schedule_warning_reset()

    timer = threading.Timer(spam_rules.WARNING_RESET_SECONDS, reset)
    timer.daemon = True
    timer.start()


def setup_spam_control_handlers(dispatcher):
    spam_rules.load_spam_words()

    dispatcher.add_handler(CommandHandler("spam", toggle_spam_control))
    dispatcher.add_handler(CallbackQueryHandler(spam_help_callback, pattern=r"^spam_help$"))
    # 优先级高于关键词回复（group 10），先过滤垃圾信息；编辑后的消息也检查
    dispatcher.add_handler(
        MessageHandler(
            (filters.TEXT | filters.CAPTION) & ~filters.COMMAND & filters.ChatType.GROUPS &
            (filters.UpdateType.MESSAGE | filters.UpdateType.EDITED_MESSAGE),
            process_message
        ),
        group=5,
    )
    # 普通群升级成超级群：AI 识别的付费状态跟着搬到新的 chat id
    dispatcher.add_handler(
        MessageHandler(filters.StatusUpdate.MIGRATE, spam_ai_handlers.migrate_chat),
        group=5,
    )

    # AI 识别的到期提醒
    dispatcher.job_queue.run_repeating(
        spam_ai_handlers.remind_expiry_job,
        interval=spam_ai_operations.REMINDER_INTERVAL_SECONDS,
        first=120,
    )

    _schedule_warning_reset()
