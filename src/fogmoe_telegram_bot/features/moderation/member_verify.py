"""`/verify` 与新成员验证：Telegram 适配层。

禁言、发欢迎消息、点【开始验证】后把欢迎消息换成九宫格小游戏、解除禁言或移出群组；开关、每一轮验证的记录与认领在
`operations/member_verify.py`，出题与判题在 `operations/verify_game.py`，文案与按钮在 `member_verify_views.py`。
群里的消息不显示新成员的名字（广告号的名字本身就是广告）；验证没通过时删掉加群消息，机器人移出成员产生的
系统消息也删掉。欢迎消息上的管理员封禁按钮由 `admin_ban.py` 处理。
"""

import logging

from telegram import ChatPermissions, Update
from telegram.constants import ChatMemberStatus
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from fogmoe_telegram_bot.core.command_cooldown import cooldown
from fogmoe_telegram_bot.core.redaction import report_error
from fogmoe_telegram_bot.features.moderation import member_verify_views as views
from fogmoe_telegram_bot.features.moderation import spam_strikes
from fogmoe_telegram_bot.features.moderation.operations import join_messages, verify_game
from fogmoe_telegram_bot.features.moderation.operations import member_verify as verification
from fogmoe_telegram_bot.features.moderation.operations.verify_game import Outcome

# 恢复任务的周期：重启后第一次在 RECOVERY_FIRST_SECONDS 秒后运行，之后每隔 RECOVERY_INTERVAL 秒。
RECOVERY_INTERVAL = 30
RECOVERY_FIRST_SECONDS = 5

# 在开启验证功能前详细检查必要权限
async def check_bot_permissions(bot, chat_id):
    bot_member = await bot.get_chat_member(chat_id, bot.id)
    if (bot_member.status not in ["administrator", "creator"]):
        return False, "机器人需要管理员权限"

    # 检查具体权限
    required_permissions = {
        "can_restrict_members": "限制成员",
    }

    missing_permissions = []
    for perm, desc in required_permissions.items():
        if not getattr(bot_member, perm, False):
            missing_permissions.append(desc)

    if missing_permissions:
        return False, f"机器人缺少以下权限: {', '.join(missing_permissions)}"

    return True, "权限检查通过"

# /verify 命令：开启新成员验证功能
@cooldown
async def verify_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    if await verification.is_enabled(chat_id):
        # 若记录存在，则只有群组管理员才能取消接管
        sender_member = await context.bot.get_chat_member(chat_id, update.effective_user.id)
        if sender_member.status not in ["administrator", "creator"]:
            await update.message.reply_text("只有群组管理员才能取消接管。")
            return
        context.chat_data["enable_verify"] = False
        await verification.disable(chat_id)
        await update.message.reply_text("验证接管已取消。")
        return

    # 仅允许群组管理员调用
    sender_member = await context.bot.get_chat_member(chat_id, update.effective_user.id)
    if sender_member.status not in ["administrator", "creator"]:
        await update.message.reply_text("只有群组管理员才能使用该命令。")
        return
    # 检查机器人是否具备管理员权限
    has_permissions, message = await check_bot_permissions(context.bot, chat_id)
    if not has_permissions:
        await update.message.reply_text(f"机器人缺少必要权限，无法开启验证功能：{message}")
        return
    context.chat_data["enable_verify"] = True
    await verification.enable(chat_id, update.effective_chat.title)
    await update.message.reply_text(
        f"新成员验证功能已开启。新成员加入时将被禁言，需要在 {verification.VERIFY_SECONDS // 60} 分钟内"
        f"点【开始验证】，在九宫格里连续 {verify_game.ROUNDS_TO_PASS} 次点对弹框提示的图标才能发言；"
        f"答错 {verify_game.MAX_MISTAKES} 次或超时会被移出群组。"
    )

# 新成员加入事件处理
async def new_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    # 所有群都记下加群消息：验证没通过、或者之后因为发垃圾信息被封禁时删掉
    for new_member in update.message.new_chat_members:
        if not new_member.is_bot:
            join_messages.JOINS.record(chat_id, new_member.id, update.message.message_id)

    if not await verification.is_enabled(chat_id):
        return

    # 同步内存状态变量（可选，为了保持一致性）
    context.chat_data["enable_verify"] = True

    for new_member in update.message.new_chat_members:
        user_id = new_member.id

        # 跳过机器人验证
        if new_member.is_bot:
            print(f"跳过机器人 {new_member.full_name}({user_id}) 的验证")
            continue

        try:
            # 禁言新成员（禁止发送消息）
            await context.bot.restrict_chat_member(
                chat_id,
                user_id,
                ChatPermissions(can_send_messages=False,
                                can_send_polls=False,
                                can_send_other_messages=False,
                                can_add_web_page_previews=False,
                                can_change_info=False,
                                can_invite_users=False,
                                can_pin_messages=False,
                                can_manage_topics=False,
                                can_send_audios=False,
                                can_send_documents=False,
                                can_send_photos=False,
                                can_send_videos=False,
                                can_send_video_notes=False,
                                can_send_voice_notes=False)
            )
        except Exception as e:
            error_str = str(e)
            notice = report_error(
                logging.getLogger(__name__), f"限制成员 {user_id} 失败", e
            )
            if "httpx.ConnectError" in error_str or "Not enough rights" in error_str:
                await context.bot.send_message(
                    chat_id,
                    f"验证错误: 无法限制成员 {user_id}，"
                    f"请检查机器人的管理员权限与网络。\n{notice}"
                )
            continue

        # 发送欢迎信息，包含开始验证按钮；不显示名字
        welcome_msg = await update.message.reply_text(
            views.welcome_text(user_id),
            reply_markup=views.welcome_keyboard(chat_id, user_id),
            parse_mode="HTML"
        )
        await verification.open_round(user_id, chat_id, welcome_msg.message_id)
        # 到点处理超时；进程重启会丢掉这个定时器，由 recover_verification_tasks 兜底
        context.job_queue.run_once(
            verification_timeout_job,
            when=verification.VERIFY_SECONDS + 0.5,
            data=(chat_id, user_id, welcome_msg.message_id),
        )

# 某一轮验证到期仍未通过：移出群组并编辑欢迎消息
async def expire_verification(bot, chat_id, user_id, message_id):
    # 已验证、已离群或已被新一轮取代时，这一轮的记录已经不在
    if not await verification.claim_round(user_id, chat_id, message_id):
        return
    verify_game.GAMES.drop(chat_id, user_id, message_id)
    await remove_unverified(bot, chat_id, user_id, message_id, views.TIMEOUT_TEXT)

# 已认领的一轮验证没通过：移出仍被禁言的成员，删掉加群消息，欢迎消息换成 text
async def remove_unverified(bot, chat_id, user_id, message_id, text):
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except Exception as e:
        print(f"查询成员 {user_id} 状态时出错: {e}")
        return
    # 只移出仍被禁言的成员，管理员已经手动放行的不动
    if member.status != ChatMemberStatus.RESTRICTED or member.can_send_messages:
        return
    try:
        # 先封禁再立即解禁：只移出，不永久封禁
        await bot.ban_chat_member(chat_id, user_id)
        await bot.unban_chat_member(chat_id, user_id)
    except Exception as e:
        print(f"踢出成员 {user_id} 时出错: {e}")
    await spam_strikes.delete_join_message(bot, chat_id, user_id)
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text
        )
    except Exception as e:
        print(f"编辑消息 {message_id} 出错: {e}")

async def verification_timeout_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id, user_id, message_id = context.job.data
    await expire_verification(context.bot, chat_id, user_id, message_id)

async def recover_verification_tasks(context: ContextTypes.DEFAULT_TYPE):
    """启动后与周期性的恢复：处理已经到期、定时器却随进程重启丢失的验证。"""
    for task in await verification.due_rounds():
        try:
            await expire_verification(context.bot, task.chat_id, task.user_id, task.message_id)
        except Exception:
            logging.getLogger(__name__).exception(
                "处理群组 %s 成员 %s 的过期验证失败", task.chat_id, task.user_id
            )

# 回调查询处理：开始验证、查看提示、点九宫格；连续答对时解除禁言，答错次数用完时移出群组
async def verify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = update.effective_chat.id

    button = views.parse_button(query.data, user_id)
    if button is None:
        await query.answer(views.NOT_YOURS, show_alert=True)
        return

    message_id = query.message.message_id if query.message else None
    game = verify_game.GAMES.get(chat_id, user_id, message_id)
    if game is None:
        # 还没开始，或者进程重启丢了这一局：这一轮验证还在就开一局
        if message_id is None or await verification.find_round_message(user_id, chat_id) != message_id:
            await answer_stale(query)
            return
        game = verify_game.GAMES.start(chat_id, user_id, message_id)
        await show_game(query, chat_id, user_id, game)
        await query.answer(views.prompt(game), show_alert=True)
        return

    if button.kind == "pick":
        outcome = verify_game.GAMES.pick(chat_id, user_id, message_id, button.question, button.cell)
    else:
        outcome = Outcome.PASSED if game.passed else None
    if outcome is Outcome.PASSED:
        await pass_verification(query, context.bot, chat_id, user_id, message_id)
        return
    if outcome is Outcome.FAILED:
        await fail_verification(query, context.bot, chat_id, user_id, message_id)
        return
    if outcome is not None or button.kind == "start":
        # 换了新题；点的是旧题或【开始验证】时，之前可能没换成功，再换一次
        await show_game(query, chat_id, user_id, game)
    await query.answer(views.prompt(game, outcome), show_alert=True)

# 欢迎消息换成这一局当前的九宫格；没换成功时，成员再点旧的按钮会再换一次
async def show_game(query, chat_id, user_id, game):
    try:
        await query.edit_message_text(
            views.game_text(user_id, game),
            reply_markup=views.game_keyboard(chat_id, user_id, game),
            parse_mode="HTML",
        )
    except Exception as e:
        print(f"显示验证小游戏出错: {e}")

# 连续答对：认领这一轮验证并解除禁言
async def pass_verification(query, bot, chat_id, user_id, message_id):
    if not await verification.claim_round(user_id, chat_id, message_id):
        # 超时或离群已经处理了这一轮，或者同时点了两下
        await query.answer(views.STALE, show_alert=True)
        return
    try:
        # 解除禁言（恢复发送消息权限）
        await bot.restrict_chat_member(
            chat_id,
            user_id,
            ChatPermissions(can_send_messages=True,
                            can_send_polls=True,
                            can_send_other_messages=True,
                            can_add_web_page_previews=True,
                            can_change_info=True,
                            can_invite_users=True,
                            can_pin_messages=True,
                            can_manage_topics=True,
                            can_send_audios=True,
                            can_send_documents=True,
                            can_send_photos=True,
                            can_send_videos=True,
                            can_send_video_notes=True,
                            can_send_voice_notes=True,)
        )
    except Exception as e:
        # 放回这一轮的记录：这一局仍算通过，用户可以再点一次，一直没通过的照常按超时处理
        await verification.open_round(user_id, chat_id, message_id)
        error_str = str(e)
        notice = report_error(
            logging.getLogger(__name__), f"解除成员 {user_id} 禁言失败", e
        )
        if "httpx.ConnectError" in error_str or "Not enough rights" in error_str:
            await bot.send_message(
                chat_id,
                f"验证错误: 无法解除禁言成员({user_id})，"
                f"请检查机器人的管理员权限与网络。\n{notice}"
            )
        await query.answer(views.UNMUTE_FAILED, show_alert=True)
        return
    # 已经解除禁言：之后改消息、弹框失败都只记一笔，不再放回这一轮
    verify_game.GAMES.drop(chat_id, user_id, message_id)
    try:
        await query.edit_message_text(views.PASSED_TEXT)
    except Exception as e:
        print(f"编辑验证消息出错: {e}")
    await answer_alert(query, views.PASSED_ALERT)

# 答错次数用完：认领这一轮验证并移出群组（不封禁）
async def fail_verification(query, bot, chat_id, user_id, message_id):
    verify_game.GAMES.drop(chat_id, user_id, message_id)
    if not await verification.claim_round(user_id, chat_id, message_id):
        await query.answer(views.STALE, show_alert=True)
        return
    # 这一轮已经认领，超时不会再处理：弹框失败也要照常移出
    await answer_alert(query, views.FAILED_ALERT)
    await remove_unverified(bot, chat_id, user_id, message_id, views.FAILED_TEXT)

# 弹框失败（网络出错、回调已过期）只记一笔，不打断验证本身的处理
async def answer_alert(query, text):
    try:
        await query.answer(text, show_alert=True)
    except Exception as e:
        print(f"回应验证按钮出错: {e}")

# 这一轮验证已经不在（已处理，或重新入群后换成了新的欢迎消息）：删掉这条旧的欢迎消息
async def answer_stale(query):
    await query.answer(views.STALE, show_alert=True)
    try:
        await query.delete_message()
    except Exception as e:
        print(f"删除验证消息时出错: {e}")

# 处理成员离开群组的事件（合并处理机器人和普通用户）
async def handle_member_left(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    service_message = update.message
    user = service_message.left_chat_member
    bot = await context.bot.get_me()

    # 如果是机器人自己被踢出
    if user.id == bot.id:
        await verification.forget_group(chat_id)
        return

    # 如果是普通成员离开，结束他未完成的验证
    message_id = await verification.find_round_message(user.id, chat_id)
    left_unverified = message_id is not None and await verification.claim_round(user.id, chat_id, message_id)
    if left_unverified:
        verify_game.GAMES.drop(chat_id, user.id, message_id)
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text="新成员在验证前离开了群组。"
            )
        except Exception as e:
            print(f"编辑消息出错: {e}")
        await spam_strikes.delete_join_message(context.bot, chat_id, user.id)

    # 「X 离开了群组」「机器人 移除了 X」也会显示名字：没验证就走的、被机器人移出的都删掉
    removed_by_bot = service_message.from_user is not None and service_message.from_user.id == bot.id
    if left_unverified or removed_by_bot:
        try:
            await service_message.delete()
        except Exception as e:
            print(f"删除离群消息出错: {e}")

# 注册该模块的处理器
def setup_member_verification(dispatcher):
    dispatcher.add_handler(CommandHandler("verify", verify_command))
    dispatcher.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, new_member_handler))
    dispatcher.add_handler(CallbackQueryHandler(verify_callback, pattern=r"^verify_"))
    dispatcher.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, handle_member_left))
    dispatcher.job_queue.run_repeating(
        recover_verification_tasks, interval=RECOVERY_INTERVAL, first=RECOVERY_FIRST_SECONDS
    )
