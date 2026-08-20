import asyncio
import base64
import logging
import time

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes, MessageHandler, filters

from core import (
    config,
    group_chat_history,
    mysql_connection,
    process_user,
    stake_reward_pool,
)
from core.archive_utils import send_permanent_records_archive
from core.prompt_utils import format_user_state_prompt
from core.telegram_history import (
    capture_telegram_history_events,
    format_user_message as _format_xml_message,
    flush_pending_events,
    normalize_command_name,
    suppress_telegram_history,
    telegram_history_scope,
)
from core.telegram_utils import partial_send, safe_send_markdown
from features.ai import ai_chat, idle_followup, summary
from features.ai.conversation_locks import get_conversation_lock
from features.ai.outbound import send_generated_media
from features.ai.reply_filter import normalize_ai_reply_text
from features.ai.sticker_sender import (
    normalize_sticker_directives,
    send_ai_reply_with_stickers,
)
from features.ai.telegram_visible_sender import TelegramVisibleContentHandler
from features.ai.tool_history import (
    tool_logs_completed_clear,
    tool_logs_to_record_entries,
)

from . import batching, lifecycle, messages, triggers
from .history_hooks import handle_history_overflow

logger = logging.getLogger(__name__)
MAX_MEDIA_DOWNLOAD_BYTES = 8 * 1024 * 1024


async def _archive_completed_clear_turn(
    *,
    bot,
    user_id: int,
    conversation_id: int,
    tool_record_entries: list[tuple[str, object]],
    assistant_message: str,
    runtime_error: str | None,
) -> None:
    """把 AI 代执行 /clear 的完整当前轮归档，并重置活跃会话。"""
    await flush_pending_events(conversation_id)
    clear_records = list(tool_record_entries)
    if assistant_message.strip() and not runtime_error:
        clear_records.append(("assistant", assistant_message))

    clear_record_id, clear_archived_records = (
        await mysql_connection.archive_chat_and_start_new_session(
            conversation_id,
            clear_records,
        )
    )

    archive_delivery_events: list[str] = []
    if clear_archived_records:
        with capture_telegram_history_events(user_id) as archive_delivery_events:
            await send_permanent_records_archive(
                bot,
                user_id,
                clear_archived_records,
                logger=logger,
            )
    if archive_delivery_events:
        await mysql_connection.append_permanent_chat_record(
            user_id,
            clear_record_id,
            [("user", content) for content in archive_delivery_events],
        )
    summary.schedule_summary_generation(conversation_id)


# 添加一个帮助函数来获取实际的消息对象


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

    batch_key = batching._message_batch_key(update)
    if not batch_key:
        await _reply_unlocked(update, context)
        return
    if config.CHAT_BATCH_WINDOW_SECONDS <= 0:
        async with get_conversation_lock(batch_key[1]):
            await _reply_unlocked(update, context)
        return

    loop = asyncio.get_running_loop()
    is_owner = False
    async with batching._MESSAGE_BATCHES_LOCK:
        batch = batching._MESSAGE_BATCHES.get(batch_key)
        if batch is None:
            future = loop.create_future()
            future.add_done_callback(batching._consume_batch_future_exception)
            batch = batching._MessageBatch(future=future)
            batching._MESSAGE_BATCHES[batch_key] = batch
            is_owner = True
        batch.items.append(batching._QueuedUpdate(update=update, context=context))
        future = batch.future

    if is_owner:
        ready_batch = None
        try:
            await asyncio.sleep(config.CHAT_BATCH_WINDOW_SECONDS)
            async with batching._MESSAGE_BATCHES_LOCK:
                ready_batch = batching._MESSAGE_BATCHES.pop(batch_key, batch)

            async with get_conversation_lock(batch_key[1]):
                await _reply_batch_unlocked(ready_batch.items)

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


async def _reply_unlocked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply_batch_unlocked([batching._QueuedUpdate(update=update, context=context)])


async def _reply_batch_unlocked(batch_items: list[batching._QueuedUpdate]) -> None:
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
    effective_message = valid_items[-1][1]
    if not effective_message:
        logging.warning("收到无效的消息更新，忽略处理")
        return

    # 如果聊天是群组，则只对包含触发词时进行回复，
    if update.effective_chat.type in ("group", "supergroup"):
        if lifecycle._BOT_ID is None:
            await lifecycle._refresh_bot_identity(
                context.bot,
                source="group message handling",
            )
        # 记录群聊上下文
        should_process_group_batch = False
        for _, message in valid_items:
            if normalize_command_name(getattr(message, "text", None)) != "fogmoebot":
                await group_chat_history.log_group_message(
                    message,
                    update.effective_chat.id,
                )
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

        if not should_process_group_batch:
            return

    # 添加：检查用户是否在聊天冷却期内
    from core.command_cooldown import check_chat_cooldown
    if not await check_chat_cooldown(update):
        return  # 用户在冷却期内，直接返回

    user_id = update.effective_user.id
    user_name = update.effective_user.username or "EmptyUsername"  # 提供默认值，防止None值导致格式化错误
    conversation_id = user_id

    pending_history_warning = None

    def remember_history_warning(level):
        nonlocal pending_history_warning
        if not level:
            return
        if pending_history_warning == "overflow":
            return
        if level == "overflow":
            pending_history_warning = "overflow"
            return
        if pending_history_warning is None:
            pending_history_warning = level

    async def notify_history_warning(level):
        if not level:
            return
        if level == "near_limit":
            warning_text = (
                "提醒：当前会话历史记录已接近系统容量上限。雾萌娘可能会在稍后自动压缩较早的消息以保持体验顺畅。"
            )
        elif level == "overflow":
            warning_text = (
                "提示：为了保证会话流畅，部分较早的聊天记录已被自动压缩保存。当前对话不受影响，若需要查看完整历史请告诉雾萌娘。"
            )
        else:
            return

        await safe_send_markdown(
            partial_send(
                context.bot.send_message,
                update.effective_chat.id,
            ),
            warning_text,
            logger=logger,
        )

    async def handle_overflow_summary(level: str | None) -> None:
        if level != "overflow":
            return
        await handle_history_overflow(conversation_id)

    async def persist_records(insert_result, *, announce: bool = False) -> None:
        """统一处理一次历史写入的收尾：归档、容量提示与摘要调度。

        announce=True 时立即把容量提示发给用户，否则先记下、由本轮末尾统一提示。
        """

        snapshot_created, warning_level, archived_records = insert_result
        if archived_records:
            await send_permanent_records_archive(
                context.bot,
                user_id,
                archived_records,
                logger=logger,
            )
        if announce:
            await notify_history_warning(warning_level)
        else:
            remember_history_warning(warning_level)
        await handle_overflow_summary(warning_level)
        if snapshot_created and warning_level != "overflow":
            summary.schedule_summary_generation(conversation_id)

    message_jobs = []
    total_coin_cost = 0
    for item, message in valid_items:
        # 如果是媒体消息（图片或贴纸），固定硬币消耗5
        if message.photo or message.sticker:
            coin_cost = 5
            is_media = True
        else:
            # 按文字消息长度阶梯计费
            user_message = message.text
            if not user_message:
                logging.warning("收到没有文本内容的消息，忽略处理")
                continue
            if len(user_message) > 4096:
                await message.reply_text("消息过长，无法处理。请缩短消息长度！\nThe message is too long to process. Please shorten the message.")
                return
            elif len(user_message) > 2000:
                coin_cost = 5
            elif len(user_message) > 1000:
                coin_cost = 4
            elif len(user_message) > 500:
                coin_cost = 3
            elif len(user_message) > 100:
                coin_cost = 2
            else:
                coin_cost = 1
            is_media = False

        message_jobs.append(
            {
                "message": message,
                "coin_cost": coin_cost,
                "is_media": is_media,
                "is_edited": item.update.edited_message is message,
            }
        )
        total_coin_cost += coin_cost

    if not message_jobs:
        return

    # 在扣费前写完上一项操作，避免余额恰好归零时把旧事件误判为收尾记录。
    await flush_pending_events(conversation_id)

    async with mysql_connection.transaction() as connection:
        row = await mysql_connection.fetch_one(
            "SELECT permission, coins, coins_paid, info FROM user WHERE id = %s",
            (user_id,),
            connection=connection,
        )
        if not row:
            await effective_message.reply_text(
                "请先使用 /me 命令注册个人信息后再聊天。\n"
                "Please register first using the /me command before chatting."
            )
            return
        user_permission = row[0]
        user_coins_free = row[1] or 0
        user_coins_paid = row[2] or 0
        user_info_raw = row[3] if len(row) > 3 else ""
        user_coins = user_coins_free + user_coins_paid

        if user_coins < total_coin_cost:
            await effective_message.reply_text(
                f"您的硬币不足，无法与雾萌娘连接，需要{total_coin_cost}个硬币。试试通过 /lottery 抽奖吧！\n"
                f"You don't have enough coins (need {total_coin_cost}), I don't want to talk to you. "
                f"Try using /lottery to get some coins!")
            return

        await process_user.spend_user_coins(
            user_id,
            total_coin_cost,
            connection=connection,
        )
        pool_add = stake_reward_pool.calculate_pool_add(total_coin_cost)
        if pool_add > 0:
            await stake_reward_pool.add_to_pool(pool_add, connection=connection)
        if user_coins_free >= total_coin_cost:
            new_free = user_coins_free - total_coin_cost
            new_paid = user_coins_paid
        else:
            remaining = total_coin_cost - user_coins_free
            new_free = 0
            new_paid = max(user_coins_paid - remaining, 0)
        user_coins = new_free + new_paid
        user_plan = process_user.resolve_user_plan(user_id, new_paid)

    user_impression_raw = await process_user.async_get_user_impression(user_id)
    impression_display = (user_impression_raw or "").strip()
    if impression_display:
        impression_display = impression_display.replace("\r", " ").replace("\n", " ")
        if len(impression_display) > 500:
            impression_display = impression_display[:497] + "..."
    else:
        impression_display = "Not recorded"

    personal_info_display = (user_info_raw or "").strip()
    if personal_info_display:
        if len(personal_info_display) > 500:
            personal_info_display = personal_info_display[:500]

    diary_row = await mysql_connection.fetch_one(
        "SELECT 1 FROM ai_user_diary_pages WHERE user_id = %s AND content != '' LIMIT 1",
        (user_id,),
    )
    diary_exists = bool(diary_row)

    user_state_prompt = format_user_state_prompt(
        user_coins=user_coins,
        user_plan=user_plan,
        user_permission=user_permission,
        impression=impression_display,
        personal_info=personal_info_display,
        diary_exists=diary_exists,
    )

    chat_type = update.effective_chat.type or "private"
    group_title = (update.effective_chat.title or "").strip() if update.effective_chat else ""
    user_record_entries = []
    runtime_replacements = []

    for job in message_jobs:
        message = job["message"]
        current_message_time = messages._format_message_timestamp(message.date) or time.strftime(
            '%Y-%m-%d %H:%M:%S'
        )
        is_edited = bool(job.get("is_edited"))
        message_metadata_kwargs = {
            "message_id": getattr(message, "message_id", None),
            "edited": is_edited,
            "edited_at": (
                messages._format_message_timestamp(getattr(message, "edit_date", None))
                if is_edited
                else None
            ),
        }
        command = normalize_command_name(getattr(message, "text", None))
        if command:
            message_metadata_kwargs.update(
                {
                    "event": "command",
                    "command": command,
                }
            )
        forward_kwargs = messages._build_forward_format_kwargs(message)
        reply_kwargs = (
            messages._build_reply_format_kwargs(message.reply_to_message)
            if message.reply_to_message
            else {}
        )

        # 如果是媒体消息，进行下载、AI分析、格式化描述
        if job["is_media"]:
            try:
                if message.photo:
                    media_type = "photo"
                    file = await message.photo[-1].get_file()
                    media_emoji = None
                else:
                    media_type = "sticker"
                    file = await message.sticker.get_file()
                    media_emoji = getattr(message.sticker, "emoji", None)

                # 检查是否有文本说明
                caption = message.caption if message.caption else ""

                file_size = getattr(file, "file_size", None)
                if file_size and file_size > MAX_MEDIA_DOWNLOAD_BYTES:
                    await message.reply_text(
                        "图片太大啦，请压缩后再发送。\n"
                        "The image is too large. Please compress it and try again."
                    )
                    return

                # 直接下载到内存，避免把用户图片落盘。
                file_bytes = await file.download_as_bytearray()
                if len(file_bytes) > MAX_MEDIA_DOWNLOAD_BYTES:
                    await message.reply_text(
                        "图片太大啦，请压缩后再发送。\n"
                        "The image is too large. Please compress it and try again."
                    )
                    return

                base64_str = base64.b64encode(file_bytes).decode('utf-8')

                # 异步调用图像分析AI
                image_description = await ai_chat.analyze_image(base64_str)

                # 组合图片描述和用户文本说明
                message_text = caption if caption else f"[{media_type}]"
                formatted_message = _format_xml_message(
                    chat_type=chat_type,
                    chat_title=group_title or None,
                    timestamp=current_message_time,
                    user_name=user_name,
                    message_text=message_text,
                    **message_metadata_kwargs,
                    **forward_kwargs,
                    **reply_kwargs,
                    media_type=media_type,
                    media_description=image_description,
                    media_emoji=media_emoji,
                )
                runtime_formatted_message = _format_xml_message(
                    chat_type=chat_type,
                    chat_title=group_title or None,
                    timestamp=current_message_time,
                    user_name=user_name,
                    message_text=message_text,
                    **message_metadata_kwargs,
                    **forward_kwargs,
                    **reply_kwargs,
                    media_type=media_type,
                    media_emoji=media_emoji,
                )
                runtime_user_message = messages._build_multimodal_user_message(
                    runtime_formatted_message,
                    base64_str=base64_str,
                    mime_type=messages._media_mime_type(media_type, message),
                )
                if runtime_user_message:
                    runtime_replacements.append(
                        (formatted_message, runtime_user_message)
                    )

            except Exception as e:
                logging.error(f"处理媒体消息时出错: {str(e)}")
                await message.reply_text(
                    "抱歉呢，雾萌娘暂时无法处理您发送的媒体，请稍后再试试看喵~\n"
                    "Sorry, I'm having trouble processing your image/sticker right now. Please try again later, meow!")
                return
        else:
            # 保留原有文本处理逻辑，处理文本消息
            user_message = message.text or ""
            formatted_message = _format_xml_message(
                chat_type=chat_type,
                chat_title=group_title or None,
                timestamp=current_message_time,
                user_name=user_name,
                message_text=user_message,
                **message_metadata_kwargs,
                **forward_kwargs,
                **reply_kwargs,
            )

        if command != "fogmoebot":
            user_record_entries.append(("user", formatted_message))

    if user_record_entries:
        # /fogmoebot 已由统一命令观察器写入；其他消息在这里批量写入。
        await persist_records(
            await mysql_connection.async_insert_chat_records(
                conversation_id,
                user_record_entries,
                system_prompt_extra=user_state_prompt,
                allow_zero_balance=True,
            )
        )
    if update.effective_chat.type == "private":
        await idle_followup.arm_from_private_turn(user_id)

    # 立即获取最新历史记录，以便AI能看到刚刚插入的消息
    chat_history = await mysql_connection.async_get_chat_history(conversation_id)

    chat_history_for_ai = messages._replace_user_messages_for_ai(
        chat_history,
        runtime_replacements,
    )

    # 异步发送"正在输入"状态
    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    except Exception:
        logger.debug("Failed to send typing action before AI request")

    # 异步获取AI回复
    tool_context = {
        "is_group": update.effective_chat.type in ("group", "supergroup"),
        "group_id": update.effective_chat.id if update.effective_chat.type in ("group", "supergroup") else None,
        "chat_id": update.effective_chat.id,
        "chat_type": update.effective_chat.type,
        "chat_title": getattr(update.effective_chat, "title", None),
        "message_id": getattr(effective_message, "message_id", None),
        "user_id": user_id,
        "username": getattr(update.effective_user, "username", None),
        "first_name": getattr(update.effective_user, "first_name", None),
        "language_code": getattr(update.effective_user, "language_code", None),
        "user_state_prompt": user_state_prompt,
    }
    sent_messages = []
    bot_event_message_ids: set[int] = set()
    fallback_send = partial_send(
        context.bot.send_message,
        update.effective_chat.id,
    )
    visible_content_handler = TelegramVisibleContentHandler(
        loop=asyncio.get_running_loop(),
        bot=context.bot,
        chat_id=update.effective_chat.id,
        first_text_send=effective_message.reply_text,
        fallback_send=fallback_send,
        logger=logger,
        reply_to_message_id=getattr(effective_message, "message_id", None),
    )

    with suppress_telegram_history():
        assistant_message, tool_logs = await ai_chat.get_ai_response(
            chat_history_for_ai,
            user_id,
            tool_context=tool_context,
            text_fallback_messages=chat_history,
            visible_content_handler=visible_content_handler,
        )
    sent_messages.extend(visible_content_handler.sent_messages)
    assistant_message = normalize_ai_reply_text(assistant_message)
    runtime_error = ai_chat.runtime_error_cause(assistant_message)
    if assistant_message.strip():
        assistant_message = await normalize_sticker_directives(
            assistant_message,
            logger=logger,
        )

    tool_record_entries = tool_logs_to_record_entries(tool_logs)
    completed_clear = tool_logs_completed_clear(tool_logs)

    if tool_record_entries and not completed_clear:
        await persist_records(
            await mysql_connection.async_insert_chat_records(
                conversation_id,
                tool_record_entries,
                allow_zero_balance=True,
            )
        )

    if assistant_message.strip() and not runtime_error and not completed_clear:
        # 异步插入AI回复到聊天记录
        await persist_records(
            await mysql_connection.async_insert_chat_record(
                conversation_id,
                "assistant",
                assistant_message,
                allow_zero_balance=True,
            )
        )

    if pending_history_warning:
        await notify_history_warning(pending_history_warning)

    # 发送未通过可见循环即时发送的最终回复
    if assistant_message.strip():
        has_visible_message = bool(sent_messages)
        try:
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        except Exception:
            logger.debug("Failed to send typing action before final AI reply")
        send_scope = (
            telegram_history_scope(
                origin="bot_runtime",
                event="error_notice",
                cause=runtime_error,
                command=normalize_command_name(getattr(effective_message, "text", None)),
            )
            if runtime_error
            else suppress_telegram_history()
        )
        with send_scope:
            reply_messages = await send_ai_reply_with_stickers(
                bot=context.bot,
                chat_id=update.effective_chat.id,
                text=assistant_message,
                first_text_send=fallback_send if has_visible_message else effective_message.reply_text,
                fallback_send=fallback_send,
                logger=logger,
                reply_to_message_id=None if has_visible_message else getattr(effective_message, "message_id", None),
            )
        sent_messages.extend(reply_messages)
        if runtime_error:
            bot_event_message_ids.update(
                message_id
                for sent_message in reply_messages
                if (message_id := getattr(sent_message, "message_id", None)) is not None
            )
    sent_messages.extend(
        await send_generated_media(
            bot=context.bot,
            chat_id=update.effective_chat.id,
            tool_logs=tool_logs,
            logger=logger,
        )
    )
    if not sent_messages and not assistant_message.strip():
        tool_log_types = [
            str(tool_log.get("type", "tool_result"))
            for tool_log in tool_logs
            if isinstance(tool_log, dict)
        ]
        logger.info(
            "AI produced empty response; no Telegram message sent: user_id=%s conversation_id=%s tool_log_types=%s",
            user_id,
            conversation_id,
            tool_log_types,
        )
    if update.effective_chat.type in ("group", "supergroup"):
        for sent_message in sent_messages:
            if sent_message is None:
                continue
            if getattr(sent_message, "message_id", None) in bot_event_message_ids:
                continue
            await group_chat_history.log_group_message(sent_message, update.effective_chat.id)

    if completed_clear:
        # 本轮工具调用完成后才建立真正的新会话边界。
        await _archive_completed_clear_turn(
            bot=context.bot,
            user_id=user_id,
            conversation_id=conversation_id,
            tool_record_entries=tool_record_entries,
            assistant_message=assistant_message,
            runtime_error=runtime_error,
        )

    # 先保存本轮所有成功显示的结果，再把零余额状态作为严格写入边界。
    await flush_pending_events(conversation_id)
    await persist_records(
        await mysql_connection.async_insert_chat_records(
            conversation_id,
            [],
            suspend_if_zero=True,
        ),
        announce=True,
    )


def setup_conversation_handlers(application) -> None:
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
