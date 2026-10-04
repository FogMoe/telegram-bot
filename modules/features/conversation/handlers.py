"""AI 对话的 Telegram 入口。

这一层只做传输相关的事：批处理窗口与会话锁、准入判断、群聊触发与冷却判断、把 `Update` 映射成
`TurnRequest`。一轮对话本身（扣费、历史、媒体、模型、投递）在 `turn.py`。

准入发生在扣费之前（规则见 docs/runtime.md 的「准入」）：

1. 每用户待处理数：同一个会话「正在处理 + 等待会话锁」的轮次有上限，超过立即拒绝；
2. 会话锁：同一个用户一次只运行一轮（等锁的时间算排队）；
3. 全局槽位：同时运行的轮次有上限，排队有最长等待；
4. 整轮截止时间：从进入队列开始计时，排队阶段到期同样走拒绝路径。

任何一步被拒绝，用户都会收到「繁忙」提示，这一轮没有开始，也不扣费。
"""

import asyncio
import logging
import time
from typing import Any

from telegram import Chat, Update, User
from telegram.ext import CommandHandler, ContextTypes, MessageHandler, filters

from core import command_cooldown, group_chat_history
from core.admission import (
    AdmissionSettings,
    OverloadReason,
    Overloaded,
    get_admission,
)
from core.deadline import REASON_SHUTDOWN, Deadline, DeadlineExceeded
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

# 过载时给用户的提示：都在扣费之前判定，所以都明确说明没有扣硬币。
BUSY_TEXT = (
    "雾萌娘现在有点忙，没能处理这条消息，请稍后再试喵～这条消息没有扣除硬币。\n"
    "I'm a bit busy right now and couldn't handle this message. "
    "Please try again shortly. You were not charged for it."
)
USER_BUSY_TEXT = (
    "你前面还有几条消息在处理中，请等雾萌娘回复之后再继续发送喵～这条消息没有扣除硬币。\n"
    "You still have several messages being processed. Please wait for the replies before "
    "sending more. You were not charged for this message."
)
SHUTTING_DOWN_TEXT = (
    "雾萌娘正在重启，请稍后再试～这条消息没有扣除硬币。\n"
    "The bot is restarting. Please try again shortly. You were not charged for this message."
)


def overload_text(reason: OverloadReason) -> str:
    if reason is OverloadReason.USER_LIMIT:
        return USER_BUSY_TEXT
    if reason is OverloadReason.SHUTTING_DOWN:
        return SHUTTING_DOWN_TEXT
    return BUSY_TEXT


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
    """排队、准入、在会话锁内处理一批消息；整个等待时间作为排队耗时交给这一轮。

    被拒绝（待处理太多、排队超时、截止时间已到、正在停止）时这一轮没有开始，用户收到提示，不扣费。
    """
    conversation_id = batch_key[1]
    deadline = Deadline.start(AdmissionSettings.from_config().turn_deadline_seconds)
    waiting_since = time.monotonic()
    try:
        with get_admission().user_pending(conversation_id, counted=_batch_is_ai_bound(items)):
            lock = get_conversation_lock(conversation_id)
            try:
                async with deadline.guard():
                    await lock.acquire()
            except DeadlineExceeded as exc:
                raise get_admission().reject(
                    OverloadReason.SHUTTING_DOWN
                    if exc.reason == REASON_SHUTDOWN
                    else OverloadReason.DEADLINE
                ) from exc
            try:
                await _reply_batch_unlocked(
                    items,
                    queue_seconds=time.monotonic() - waiting_since,
                    deadline=deadline,
                )
            finally:
                lock.release()
    except Overloaded as exc:
        await _reject_unstarted_batch(items, exc.reason)


async def _send_overload_notice(message: Any, reason: OverloadReason) -> None:
    try:
        await message.reply_text(overload_text(reason))
    except Exception:
        logger.debug("Failed to send the overload notice", exc_info=True)


async def _reject_unstarted_batch(
    items: list[batching._QueuedUpdate],
    reason: OverloadReason,
) -> None:
    """在会话锁之外被拒绝的批次：群聊消息仍要记进群聊上下文，然后提示用户。"""
    valid_items = []
    for item in items:
        message = messages.get_effective_message(item.update)
        if message:
            valid_items.append((item, message))
    if not valid_items:
        return
    valid_items.sort(key=batching._batch_item_sort_key)

    update = valid_items[-1][0].update
    chat = update.effective_chat
    if chat is not None and chat.type in ("group", "supergroup"):
        await _log_group_messages(valid_items, chat.id)
    await _send_overload_notice(valid_items[-1][1], reason)


async def _reply_unlocked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply_batch_unlocked([batching._QueuedUpdate(update=update, context=context)])


def _chat_and_user(update: Update) -> tuple[Chat, User]:
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None:
        raise ValueError("conversation update has no chat or user")
    return chat, user


def _message_addresses_bot(message: Any) -> bool:
    """群聊消息是否点名了 bot：回复 bot 的消息，或带直接触发词。纯判断，没有副作用。"""
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
        return True
    return triggers.message_contains_direct_ai_trigger(message)


def _batch_is_ai_bound(items: list[batching._QueuedUpdate]) -> bool:
    """这一批消息是否会走到模型：私聊总是；群聊要有消息点名了 bot。

    只在准入阶段用来决定是否计入「每用户待处理数」：不会唤起 AI 的群聊消息不占名额，
    也不会因为用户的 AI 轮次排得太满而被拒绝。纯判断，没有副作用。
    """
    for item in items:
        message = messages.get_effective_message(item.update)
        chat = item.update.effective_chat
        if not message or chat is None:
            continue
        if chat.type not in ("group", "supergroup"):
            return True
        if _message_addresses_bot(message):
            return True
    return False


async def _log_group_messages(valid_items: list, chat_id: int) -> None:
    for _, message in valid_items:
        if normalize_command_name(getattr(message, "text", None)) != "fogmoebot":
            try:
                await group_chat_history.log_group_message(message, chat_id)
            except Exception:
                logger.exception("记录群聊消息失败: group_id=%s", chat_id)


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
        if _message_addresses_bot(message):
            should_process_group_batch = True
    return should_process_group_batch


def _build_turn_request(
    valid_items: list,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    queue_seconds: float,
    deadline: Deadline | None = None,
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
        deadline=deadline,
    )


async def _reply_batch_unlocked(
    batch_items: list[batching._QueuedUpdate],
    *,
    queue_seconds: float = 0.0,
    services: TurnServices | None = None,
    settings: ConversationSettings | None = None,
    deadline: Deadline | None = None,
) -> None:
    """把一批已排好队的 Telegram 更新交给一轮对话：触发与冷却判断、全局准入、输入映射，然后运行业务操作。"""
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

    # 全局准入：在扣费之前拿到槽位；拿不到就提示用户，这一轮没有开始，不扣费。
    if deadline is None:
        deadline = Deadline.start(AdmissionSettings.from_config().turn_deadline_seconds)
    try:
        async with get_admission().slot(deadline=deadline) as slot:
            await run_turn(
                _build_turn_request(
                    valid_items,
                    update,
                    context,
                    queue_seconds + slot.waited,
                    deadline,
                ),
                services,
                settings,
            )
    except Overloaded as exc:
        await _send_overload_notice(valid_items[-1][1], exc.reason)


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
