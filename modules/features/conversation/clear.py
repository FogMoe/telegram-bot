import logging

from telegram import Update
from telegram.ext import ContextTypes

from core import mysql_connection, process_user
from core.archive_utils import send_permanent_records_archive
from core.command_cooldown import cooldown
from core.telegram_history import (
    capture_telegram_history_events,
    flush_pending_events,
    record_command_update,
    telegram_history_capture_active,
    telegram_history_scope,
)
from features.ai import idle_followup, summary
from features.ai.conversation_locks import get_conversation_lock

logger = logging.getLogger(__name__)


async def _clear_command_unlocked(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    user_id: int,
    conversation_id: int,
    delegated_capture: bool,
) -> None:
    # 清空前先写完旧会话尚在限流窗口内的事件，避免它们落入新会话。
    await flush_pending_events(user_id)

    if await process_user.async_get_user_coins(user_id) < 1:
        with telegram_history_scope(
            origin="bot_runtime",
            event="error_notice",
            cause="coins_insufficient",
            command="clear",
        ):
            await update.message.reply_text(
                "您的硬币不足，无法与雾萌娘连接，需要1个硬币。"
                "试试通过 /lottery 抽奖吧！"
            )
        return

    await idle_followup.cancel_idle_followup(user_id)

    # AI 代执行时由当前 AI 轮在工具记录完整后统一归档，避免拆散旧会话。
    if delegated_capture:
        await record_command_update(update, context.bot)
        await update.message.reply_text(
            "雾萌娘已进行记忆清除处理。\n"
            "The current conversation history has been cleared."
        )
        return

    # 普通 /clear 先把命令放进旧会话归档；新活跃历史只留下 new_session。
    with capture_telegram_history_events(user_id) as command_events:
        await record_command_update(update, context.bot)

    record_id, archived_records = (
        await mysql_connection.archive_chat_and_start_new_session(
            conversation_id,
            [("user", content) for content in command_events],
        )
    )

    post_clear_events: list[str] = []
    try:
        with capture_telegram_history_events(user_id) as post_clear_events:
            await update.message.reply_text(
                "雾萌娘已进行记忆清除处理。\n"
                "The current conversation history has been cleared."
            )
            if archived_records:
                await send_permanent_records_archive(
                    context.bot,
                    user_id,
                    archived_records,
                    logger=logger,
                )
    finally:
        await mysql_connection.append_permanent_chat_record(
            user_id,
            record_id,
            [("user", content) for content in post_clear_events],
        )

    summary.schedule_summary_generation(user_id)


@cooldown
async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    conversation_id = user_id
    delegated_capture = telegram_history_capture_active(user_id)
    if delegated_capture:
        # AI 工具调用已处于当前会话锁内，不能重复获取同一把锁。
        await _clear_command_unlocked(
            update,
            context,
            user_id=user_id,
            conversation_id=conversation_id,
            delegated_capture=True,
        )
        return

    async with get_conversation_lock(conversation_id):
        await _clear_command_unlocked(
            update,
            context,
            user_id=user_id,
            conversation_id=conversation_id,
            delegated_capture=False,
        )
