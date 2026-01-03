import asyncio
from core import mysql_connection, process_user
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from core.command_cooldown import cooldown

# 任务ID
TASK_ID_CHECK_GROUP1 = 1  # 任务1：加入 @ScarletKc_Group 群组
TASK_ID_CHECK_GROUP2 = 2  # 任务2：加入 @FOG_MOE 群组

# 指定目标群组ID（使用群组ID，此格式适用于 Telegram API）
TARGET_GROUP_ID1 = -1001870858408  # 替换为 @ScarletKc_Group 实际群组 ID
TARGET_GROUP_ID2 = -1002053007005  # 替换为 @FOG_MOE 实际群组 ID

# 用于提示展示，可用群组用户名或名称
TASK_NAME_1 = "@ScarletKc_Group"
TASK_NAME_2 = "@FOG_MOE"

# 奖励硬币数，可根据需求设置
REWARD_COINS_1 = 10
REWARD_COINS_2 = 10

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
        except Exception:
            pass
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
    row = await mysql_connection.fetch_one(
        "SELECT 1 FROM user_task WHERE user_id = %s AND task_id = %s",
        (user_id, task_id),
    )
    if row:
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
        async with mysql_connection.transaction() as connection:
            await process_user.add_free_coins(
                user_id,
                reward_coins,
                connection=connection,
            )
            await connection.exec_driver_sql(
                "INSERT INTO user_task (user_id, task_id) VALUES (%s, %s)",
                (user_id, task_id),
            )
        await query.answer(f"恭喜您完成任务，获得 {reward_coins} 个硬币奖励！", show_alert=True)
    except Exception:
        await query.answer("发放奖励时出现错误，请稍后再试。", show_alert=True)
