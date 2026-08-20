import logging

from telegram import Update
from telegram.ext import ChatMemberHandler, ContextTypes
from sqlalchemy.exc import SQLAlchemyError

from core import config, mysql_connection, process_user
from core.command_cooldown import cooldown
from core.telegram_utils import partial_send, safe_send_markdown
from features.economy import ref

logger = logging.getLogger(__name__)


@cooldown
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 检查是否有启动参数（推广邀请码）
    if context.args:
        # 处理推广系统的邀请链接
        await ref.process_start_with_args(update, context)

    # 显示欢迎消息
    await context.bot.send_message(chat_id=update.effective_chat.id, text="欢迎使用雾萌机器人喵！！我是雾萌娘，有什么可以帮到您的吗？输入 /help "
                                                                       "我会尽力帮助您的哦。\n"
                                                                       "Welcome to the FogMoeBot! Meow! I'm "
                                                                       "your assistant, is there anything I can "
                                                                       "help you "
                                                                       "with? Type /help and I'll do my best.")


@cooldown
async def me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_name = update.effective_user.username

    # 检查用户名是否为空
    if not user_name:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="您需要设置Telegram用户名才能使用机器人。\n"
                 "请在Telegram设置中设置用户名后再尝试。\n\n"
                 "You need to set a Telegram username to use this bot.\n"
                 "Please set your username in Telegram settings and try again."
        )
        return

    try:
        insert_query = (
            "INSERT INTO user (id, name, coins) VALUES (%s, %s, %s) "
            "ON DUPLICATE KEY UPDATE name = VALUES(name)"
        )
        select_query = "SELECT coins, coins_paid, permission, user_plan FROM user WHERE id = %s"

        async with mysql_connection.transaction() as connection:
            await connection.exec_driver_sql(
                insert_query,
                (user_id, user_name, config.NEW_USER_BONUS_COINS),
            )
            result = await connection.exec_driver_sql(select_query, (user_id,))
            row = result.fetchone()
            user_coins_free = row[0] if row else 0
            user_coins_paid = row[1] if row else 0
            user_permission = row[2] if row else 0
            user_plan_db = row[3] if row and len(row) > 3 else ""
            user_coins_total = user_coins_free + user_coins_paid
            user_plan = process_user.resolve_user_plan(user_id, user_coins_paid)
            if user_plan_db != user_plan:
                await connection.exec_driver_sql(
                    "UPDATE user SET user_plan = %s WHERE id = %s",
                    (user_plan, user_id),
                )
    except SQLAlchemyError as err:
        logging.error(f"数据库错误: {err}")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="发生错误，请稍后再试。\nAn error occurred, please try again later."
        )
        return

    await safe_send_markdown(
        update.message.reply_text,
        (
            f"👤 *用户信息 User Info*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"用户名 Name: @{user_name}\n"
            f"权限 Permission: {user_permission}\n"
            f"方案 Plan: {user_plan}\n\n"
            f"💰 *金币资产 Coins Balance*\n"
            f"• 总额 Total: {user_coins_total}\n"
            f"• 免费 Free: {user_coins_free}\n"
            f"• 付费 Paid: {user_coins_paid}"
        ),
        logger=logger,
        fallback_send=partial_send(
            context.bot.send_message,
            update.effective_chat.id,
        ),
    )


@cooldown
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = config.HELP_TEXT
    await safe_send_markdown(
        update.message.reply_text,
        help_text,
        logger=logger,
        fallback_send=partial_send(
            context.bot.send_message,
            update.effective_chat.id,
        ),
        disable_web_page_preview=True,
    )


@cooldown
async def github_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send repository link with Markdown formatting."""
    await safe_send_markdown(
        update.message.reply_text,
        "***Open Source***:\n"
        "[AGPL3.0](https://github.com/FogMoe/telegram-bot)",
        logger=logger,
        fallback_send=partial_send(
            context.bot.send_message,
            update.effective_chat.id,
        ),
    )


@cooldown
async def setmyinfo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    current_row = await mysql_connection.fetch_one(
        "SELECT info FROM user WHERE id = %s",
        (user_id,),
    )
    current_info = current_row[0] if current_row else "无"
    await update.message.reply_text(f"您当前保存的个人自定义信息是Your current personal info is:\n{current_info}")

    if not context.args:
        await update.message.reply_text(
            "请在 /setmyinfo 命令后输入要您要保存的个人自定义信息，会在后续对话中生效。\n"
            "The personal information you want to save should be entered after the command and will be used in subsequent conversations.\n\n"
            "在命令后输入CLEAR可以清空个人自定义信息（例如/setmyinfo CLEAR ）。\n"
            "Enter CLEAR after the command to clear the personal information.(e.g./setmyinfo CLEAR)"
        )
        return

    user_info = " ".join(context.args)

    # 如果用户输入CLEAR，则清空info
    if user_info.strip().upper() == "CLEAR":
        user_info = ""

    if len(user_info) > 500:
        await update.message.reply_text("最长500个字符，个人自定义信息长度超过500字符，请重试。\nThe maximum length is 500 characters, the personal information length exceeds 500 characters, please try again.")
        return

    await mysql_connection.execute(
        "UPDATE user SET info = %s WHERE id = %s",
        (user_info, user_id),
    )
    await update.message.reply_text("个人自定义信息已更新。\nPersonal information has been updated.")


async def my_chat_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 当机器人的 chat member 状态更新时触发
    result = update.my_chat_member
    old_status = result.old_chat_member.status
    new_status = result.new_chat_member.status
    bot = await context.bot.get_me()
    # 判断更新是否为自己，并且状态从非成员变为成员或管理员
    if result.new_chat_member.user.id == bot.id and old_status in ["left", "kicked"] and new_status in ["member", "administrator", "creator"]:
        # 调用 /start 命令中的欢迎消息逻辑
        await start(update, context)


def setup_membership_handlers(application) -> None:
    """机器人自身的群成员状态变化：入群时复用 /start 的欢迎流程。"""

    application.add_handler(
        ChatMemberHandler(
            my_chat_member_handler,
            chat_member_types=ChatMemberHandler.MY_CHAT_MEMBER,
        )
    )
