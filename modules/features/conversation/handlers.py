"""AI 对话的 Telegram 入口。

这一层只做传输相关的事：批处理窗口与会话锁、群聊触发与冷却判断、把 `Update` 映射成
`TurnRequest`。一轮对话本身（扣费、历史、媒体、模型、投递）在 `turn.py`。
"""

import asyncio
import logging
import time
from typing import Any

from telegram import Chat, Update, User
from telegram.ext import CommandHandler, ContextTypes, MessageHandler, filters

from core import command_cooldown, group_chat_history
from core.telegram_history import normalize_command_name
from features.ai import idle_followup
from features.ai.conversation_locks import get_conversation_lock

from . import batching, lifecycle, messages, triggers
from .turn import run_turn
from .turn_services import TurnServices
from .turn_types import (
    ChatRef,
    ConversationSettings,
    IncomingMessage,
    SenderRef,
    TurnRequest,
)

logger = logging.getLogger(__name__)


async def reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if messages._record_message_content_and_check_unchanged_edit(update):
        logger.debug(
            "Ignoring edited message with unchanged AI-visible content: chat_id=%s message_id=%s update_id=%s",
            getattr(update.effective_chat, "id", None),
            getattr(update.edited_message, "message_id", None),
            getattr(update, "update_id", None),
        )
        return

    if (
        update.effective_chat
        and update.effective_chat.type == "private"
        and update.effective_user
    ):
        await idle_followup.note_incoming_private_message(update.effective_user.id)

    batch_window = ConversationSettings.from_config().batch_window_seconds
    batch_key = batching._message_batch_key(update)
    if not batch_key:
        await _reply_unlocked(update, context)
        return
    if batch_window <= 0:
        await _reply_locked(
            batch_key,
            [batching._QueuedUpdate(update=update, context=context)],
        )
        return

    loop = asyncio.get_running_loop()
    is_owner = False
    async with batching._MESSAGE_BATCHES_LOCK:
        batch = batching._MESSAGE_BATCHES.get(batch_key)
        if batch is None:
            new_future = loop.create_future()
            new_future.add_done_callback(batching._consume_batch_future_exception)
            batch = batching._MessageBatch(future=new_future)
            batching._MESSAGE_BATCHES[batch_key] = batch
            is_owner = True
        batch.items.append(batching._QueuedUpdate(update=update, context=context))
        future = batch.future

    if is_owner:
        ready_batch = None
        try:
            await asyncio.sleep(batch_window)
            async with batching._MESSAGE_BATCHES_LOCK:
                ready_batch = batching._MESSAGE_BATCHES.pop(batch_key, batch)

            await _reply_locked(batch_key, ready_batch.items)

            if future and not future.done():
                future.set_result(None)
        except BaseException as exc:
            if future and not future.done():
                future.set_exception(exc)
            raise
        finally:
            async with batching._MESSAGE_BATCHES_LOCK:
                if batching._MESSAGE_BATCHES.get(batch_key) is batch:
                    batching._MESSAGE_BATCHES.pop(batch_key, None)
        return

    if future:
        await asyncio.shield(future)


async def _reply_locked(
    batch_key: tuple[int, int],
    items: list[batching._QueuedUpdate],
) -> None:
    """在会话锁内处理一批消息；等锁的时间作为排队耗时交给这一轮。"""
    waiting_since = time.monotonic()
    async with get_conversation_lock(batch_key[1]):
        await _reply_batch_unlocked(
            items,
            queue_seconds=time.monotonic() - waiting_since,
        )


async def _reply_unlocked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply_batch_unlocked([batching._QueuedUpdate(update=update, context=context)])


def _chat_and_user(update: Update) -> tuple[Chat, User]:
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None:
        raise ValueError("conversation update has no chat or user")
    return chat, user


async def _group_batch_wants_a_reply(
    valid_items: list,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    """群聊里先把每条消息记进群聊上下文，再判断这一批是否要唤起 AI。"""
    chat, _ = _chat_and_user(update)
    if lifecycle._BOT_ID is None:
        await lifecycle._refresh_bot_identity(
            context.bot,
            source="group message handling",
        )
    should_process_group_batch = False
    for _, message in valid_items:
        if normalize_command_name(getattr(message, "text", None)) != "fogmoebot":
            await group_chat_history.log_group_message(message, chat.id)
        reply_from_user = getattr(
            getattr(message.reply_to_message, "from_user", None),
            "id",
            None,
        )
        if (
            message.reply_to_message
            and lifecycle._BOT_ID is not None
            and reply_from_user == lifecycle._BOT_ID
        ):
            should_process_group_batch = True
            continue

        if triggers.message_contains_direct_ai_trigger(message):
            should_process_group_batch = True
    return should_process_group_batch


def _build_turn_request(
    valid_items: list,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    queue_seconds: float,
) -> TurnRequest:
    chat, user = _chat_and_user(update)
    return TurnRequest(
        chat=ChatRef(
            chat_id=chat.id,
            chat_type=chat.type,
            title=getattr(chat, "title", None),
        ),
        sender=SenderRef(
            user_id=user.id,
            username=getattr(user, "username", None),
            first_name=getattr(user, "first_name", None),
            language_code=getattr(user, "language_code", None),
        ),
        messages=tuple(
            IncomingMessage(
                message=message,
                edited=item.update.edited_message is message,
                update_id=getattr(item.update, "update_id", None),
            )
            for item, message in valid_items
        ),
        bot=context.bot,
        queue_seconds=queue_seconds,
    )


async def _reply_batch_unlocked(
    batch_items: list[batching._QueuedUpdate],
    *,
    queue_seconds: float = 0.0,
    services: TurnServices | None = None,
    settings: ConversationSettings | None = None,
) -> None:
    """把一批已排好队的 Telegram 更新交给一轮对话：准入判断、输入映射，然后运行业务操作。"""
    if not batch_items:
        return

    valid_items = []
    for item in batch_items:
        message = messages.get_effective_message(item.update)
        if not message:
            logging.warning("收到无效的消息更新，忽略处理")
            continue
        valid_items.append((item, message))

    if not valid_items:
        return
    valid_items.sort(key=batching._batch_item_sort_key)

    update = valid_items[-1][0].update
    context = valid_items[-1][0].context

    # 如果聊天是群组，则只对包含触发词时进行回复，
    chat, _ = _chat_and_user(update)
    if chat.type in ("group", "supergroup"):
        if not await _group_batch_wants_a_reply(valid_items, update, context):
            return

    # 检查用户是否在聊天冷却期内；冷却期内直接返回
    if not await command_cooldown.check_chat_cooldown(update):
        return

    await run_turn(
        _build_turn_request(valid_items, update, context, queue_seconds),
        services,
        settings,
    )


def setup_conversation_handlers(application: Any) -> None:
    """注册 AI 对话入口：显式命令与被动消息两条路径共用同一个 handler。"""

    application.add_handler(CommandHandler("fogmoebot", reply))
    application.add_handler(
        MessageHandler(
            (filters.TEXT | filters.PHOTO | filters.Sticker.ALL)
            & ~filters.COMMAND
            & ~filters.VIA_BOT
            & (filters.UpdateType.MESSAGE | filters.UpdateType.EDITED_MESSAGE),
            reply,
        )
    )
