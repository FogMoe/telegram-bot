"""/ref 与 /start 邀请链接的 Telegram 适配层：输入映射与回复。邀请规则与事务在 `operations/invitations.py`。"""

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from fogmoe_telegram_bot.core import config
from fogmoe_telegram_bot.core.command_cooldown import cooldown

from .operations import invitations as invitation_operations
from .operations.invitations import INVITATION_REWARD, invited_user_reward

# 配置logger
logger = logging.getLogger(__name__)

# GROUP_REWARD = 20
# MIN_GROUP_MEMBERS = 20

async def process_start_with_args(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理带参数的/start命令，用于推广系统的邀请链接"""
    user_id = update.effective_user.id
    user_name = update.effective_user.full_name

    # 获取启动参数（邀请人ID）
    try:
        referrer_id = int(context.args[0])
    except (ValueError, IndexError):
        return False

    # 检查是否是自己邀请自己
    if user_id == referrer_id:
        return False

    # 添加邀请记录，并给双方发放奖励
    outcome = await invitation_operations.add_invitation_record(
        user_id,
        referrer_id,
        user_name,
    )
    if outcome.recorded:
        if outcome.new_user:
            reward_message = (
                f"🎁 您已通过邀请链接加入，获得了 *{INVITATION_REWARD}* 邀请奖励 + "
                f"*{config.NEW_USER_BONUS_COINS}* 新人奖励（共 *{invited_user_reward()}* 金币）！"
            )
        else:
            reward_message = f"🎁 您已通过邀请链接加入，获得了 *{INVITATION_REWARD}* 邀请奖励！"

        try:
            # 获取邀请人的用户名
            referrer_name = await invitation_operations.get_user_name(referrer_id)

            # 获取邀请人的Telegram用户名（如果可能）
            try:
                # 尝试直接获取用户信息
                chat = await context.bot.get_chat(referrer_id)
                if chat and chat.username:
                    referrer_display = f"@{chat.username}"
                elif referrer_name:
                    referrer_display = f"{referrer_name} (`{referrer_id}`)"
                else:
                    referrer_display = f"`{referrer_id}`"
            except Exception as e:
                # 如果无法获取Telegram用户信息，使用数据库中的名称
                logger.error(f"Error getting chat for user {referrer_id}: {e}")
                if referrer_name:
                    referrer_display = f"{referrer_name} (`{referrer_id}`)"
                else:
                    referrer_display = f"`{referrer_id}`"

            # 向被邀请用户发送欢迎消息，使用Markdown格式
            await update.message.reply_text(
                f"{reward_message}\n"
                f"您的邀请人是：{referrer_display}",
                parse_mode=ParseMode.MARKDOWN
            )
            return True
        except Exception as e:
            logger.error(f"Error in process_start_with_args when sending message: {e}")
            # 如果获取用户名或发送消息失败，使用原始ID
            await update.message.reply_text(
                f"{reward_message}\n"
                f"您的邀请人是：`{referrer_id}`",
                parse_mode=ParseMode.MARKDOWN
            )
            return True
    return False

@cooldown
async def ref_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理/ref命令，根据是否有参数执行不同的功能"""
    if not context.args:
        # 没有参数，显示用户的邀请信息
        try:
            user_id = update.effective_user.id
            # 从数据库获取该用户邀请的信息
            summary = await invitation_operations.get_invitation_summary(user_id)

            # 获取当前用户的邀请人信息
            referrer = await invitation_operations.get_referrer(user_id)

            # 生成邀请链接
            bot_username = (await context.bot.get_me()).username
            invite_link = f"https://t.me/{bot_username}?start={user_id}"

            # 准备回复消息，使用Markdown格式
            message = (
                f"🎉 *您的邀请信息* 🎉\n\n"
                f"📊 已邀请人数：*{summary.count}*\n"
                f"💰 已获得奖励：*{summary.count * INVITATION_REWARD}* 金币\n\n"
            )

            # 如果有邀请人，显示邀请人信息
            if referrer:
                message += f"👤 您的邀请人：*{referrer.name}* (`{referrer.user_id}`)\n\n"

            message += (
                f"您的邀请码：`{user_id}`\n\n"  # 使用代码块格式，方便用户点击复制
                f"🔗 您的专属邀请链接：\n`{invite_link}`\n\n"  # 使用代码块格式，方便用户点击复制
                f"将此链接分享给好友，当他们点击链接并启动机器人时，您将获得 *{INVITATION_REWARD}* 金币奖励！\n\n"
                f"✨ *邀请规则：*\n"
                f"- 每邀请一位新用户，您将获得 *{INVITATION_REWARD}* 金币奖励\n"
                f"- 被邀请用户也将获得 *{INVITATION_REWARD}* 邀请奖励 + "
                f"*{config.NEW_USER_BONUS_COINS}* 新人奖励（共 *{invited_user_reward()}*）\n"
                f"- 每个Telegram账号只能被邀请一次\n\n"
                # f"- 将机器人添加到 *{MIN_GROUP_MEMBERS}* 人以上的群组，可获得 *{GROUP_REWARD}* 金币奖励\n\n"
                f"如需手动绑定邀请人，请使用命令：`/ref <邀请码>`\n"
                f"例如：`/ref {user_id}`"  # 使用用户自己的ID作为示例
            )

            # 如果有邀请的用户，列出前10个
            if summary.invited:
                message += "\n\n🙋‍♂️ *最近邀请的用户（最多显示10个）：*\n"
                for idx, invited in enumerate(summary.invited[:10], 1):
                    message += f"{idx}. {invited.name} (`{invited.user_id}`) - {invited.invited_at.strftime('%Y-%m-%d %H:%M:%S')}\n"

            await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            logger.error(f"Error in ref_command (show info): {e}")
            await update.message.reply_text("获取邀请信息时出错，请稍后再试。")
        return

    # 有参数，执行绑定邀请人功能
    try:
        referrer_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("邀请码必须是数字！")
        return

    try:
        # 检查是否是自己邀请自己
        if update.effective_user.id == referrer_id:
            await update.message.reply_text("您不能邀请自己哦！")
            return

        # 检查用户是否已经被邀请过
        user_id = update.effective_user.id
        current_referrer = await invitation_operations.get_referrer(user_id)
        if current_referrer:
            await update.message.reply_text(
                f"绑定失败，您已经被 *{current_referrer.name}* (`{current_referrer.user_id}`) 邀请过了。每个用户只能被邀请一次。",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        # 添加邀请记录，并给双方发放奖励
        outcome = await invitation_operations.add_invitation_record(
            user_id,
            referrer_id,
            update.effective_user.full_name,
        )
        if outcome.recorded:
            if outcome.new_user:
                reward_message = (
                    f"邀请绑定成功！您获得了 *{INVITATION_REWARD}* 邀请奖励 + "
                    f"*{config.NEW_USER_BONUS_COINS}* 新人奖励（共 *{invited_user_reward()}* 金币）！"
                )
            else:
                reward_message = f"邀请绑定成功！您获得了 *{INVITATION_REWARD}* 邀请奖励！"
            await update.message.reply_text(reward_message, parse_mode=ParseMode.MARKDOWN)
        else:
            # 检查邀请人是否存在
            referrer_exists = await invitation_operations.user_exists(referrer_id)
            if not referrer_exists:
                await update.message.reply_text("邀请绑定失败，邀请人不存在。请检查邀请码是否正确。")
            else:
                await update.message.reply_text("邀请绑定失败，可能是系统错误。请稍后再试。")
    except Exception as e:
        logger.error(f"Error in ref_command (bind referrer): {e}")
        await update.message.reply_text("处理邀请绑定时出错，请稍后再试。")

async def ref_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理推广系统的按钮回调"""
    query = update.callback_query
    await query.answer()

    # 因为移除了复制邀请链接按钮，此函数可以保留以备将来扩展，但目前不做任何操作
    pass


def setup_ref_handlers(application):
    """设置推广系统的命令处理器"""
    # 只添加ref命令，移除myref命令
    application.add_handler(CommandHandler("ref", ref_command))

    # 保留回调处理器以备将来扩展
    application.add_handler(CallbackQueryHandler(ref_callback, pattern=r"^ref_"))
