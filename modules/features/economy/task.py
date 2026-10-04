"""/task 的 Telegram 适配层：菜单、检查群成员身份、回复。任务定义与领取在 `operations/task.py`。"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from core.command_cooldown import cooldown
from core.redaction import log_exception

from .operations import task as task_operations
from .operations.task import (
    REWARD_COINS_1,
    REWARD_COINS_2,
    TARGET_GROUP_ID1,
    TARGET_GROUP_ID2,
    TASK_ID_CHECK_GROUP1,
    TASK_ID_CHECK_GROUP2,
    TASK_NAME_1,
    TASK_NAME_2,
    TaskClaim,
)

logger = logging.getLogger(__name__)


@cooldown
async def task_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /task 命令：发送任务菜单，展示可领取任务
    """
    keyboard = [
        [InlineKeyboardButton("领取@ScarletKc_Group任务1奖励 - 10金币", callback_data="task_check_group1")],
        [InlineKeyboardButton("领取@FOG_MOE任务2奖励 - 10金币", callback_data="task_check_group2")],
        [InlineKeyboardButton("关闭任务窗口", callback_data="task_close")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = (
        "任务中心：\n"
        f"任务1：请先加入我们的指定群组 {TASK_NAME_1}，完成后领取10个硬币奖励。\n"
        f"任务2：请先加入我们的指定群组 {TASK_NAME_2}，完成后领取10个硬币奖励。"
    )
    await update.message.reply_text(text, reply_markup=reply_markup)

async def task_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    处理任务按钮回调：
    - task_check_group1：检测用户是否在群组 TARGET_GROUP_ID1 中
      * 若已完成任务，则提示任务已完成
      * 若不在群组，提示加入群组后再领取
      * 若在群组且未完成任务，则发放奖励，并在数据库记录任务完成
    - task_check_group2：检测用户是否在群组 TARGET_GROUP_ID2 中，逻辑同上
    - task_close：删除任务消息
    """
    query = update.callback_query
    user_id = query.from_user.id

    if query.data == "task_close":
        try:
            await query.delete_message()
        except Exception as exc:
            logger.debug("关闭任务消息失败: %s", exc)
        return

    # 根据不同任务设置相应参数
    if query.data == "task_check_group1":
        task_id = TASK_ID_CHECK_GROUP1
        target_group = TARGET_GROUP_ID1
        reward_coins = REWARD_COINS_1
        task_name = TASK_NAME_1
    elif query.data == "task_check_group2":
        task_id = TASK_ID_CHECK_GROUP2
        target_group = TARGET_GROUP_ID2
        reward_coins = REWARD_COINS_2
        task_name = TASK_NAME_2
    else:
        return

    # 检查任务是否已完成
    if await task_operations.is_task_completed(user_id, task_id):
        await query.answer("您已完成该任务，不能重复领取奖励。", show_alert=True)
        return

    # 调用 Telegram API 检查用户在目标群组中的状态
    try:
        member = await context.bot.get_chat_member(chat_id=target_group, user_id=user_id)
        # 当状态为 left 或 kicked 时，视为未加入群组
        if member.status in ["left", "kicked"]:
            await query.answer(f"检测到您尚未加入 {task_name} 群组，请先加入再领取奖励。", show_alert=True)
            return
    except Exception:
        await query.answer("无法验证您是否在指定群组，请稍后再试。", show_alert=True)
        return

    # 发放奖励并记录任务完成
    try:
        status = await task_operations.claim_task_reward(user_id, task_id, reward_coins)
    except Exception:
        log_exception(logger, f"发放任务奖励失败: user_id={user_id} task_id={task_id}")
        await query.answer("发放奖励时出现错误，请稍后再试。", show_alert=True)
        return

    if status is TaskClaim.NOT_REGISTERED:
        await query.answer("请先使用 /me 命令获取个人信息。", show_alert=True)
    elif status is TaskClaim.ALREADY_DONE:
        await query.answer("您已完成该任务，不能重复领取奖励。", show_alert=True)
    else:
        await query.answer(f"恭喜您完成任务，获得 {reward_coins} 个硬币奖励！", show_alert=True)


def setup_task_handlers(application) -> None:
    """注册任务系统的命令与回调。"""

    application.add_handler(CommandHandler("task", task_command))
    application.add_handler(CallbackQueryHandler(task_callback, pattern=r"^task_"))
