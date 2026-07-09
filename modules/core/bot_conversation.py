import asyncio
import base64
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field

import telegram
from telegram import Update
from telegram.ext import ContextTypes

from core import config, db, group_chat_history, mysql_connection, process_user, stake_reward_pool
from core.archive_utils import send_permanent_records_archive
from core.prompt_utils import format_metadata_attrs, format_user_state_prompt, xml_escape
from core.telegram_utils import (
    describe_forward_for_context,
    describe_message_for_context,
    partial_send,
    safe_send_markdown,
)
from features.ai import ai_chat, summary
from features.ai.conversation_locks import get_conversation_lock
from features.ai.generated_audio_sender import send_generated_audio_from_tool_logs
from features.ai.generated_image_sender import send_generated_images_from_tool_logs
from features.ai.reply_filter import normalize_ai_reply_text
from features.ai.sticker_sender import normalize_sticker_directives, send_ai_reply_with_stickers
from features.ai.telegram_visible_sender import TelegramVisibleContentHandler
from features.ai.task_runner import run_ai_task
from features.ai.tool_history import tool_logs_to_record_entries

logger = logging.getLogger(__name__)


_BOT_ID: int | None = None
_BOT_USERNAME: str = "FogMoeBot"
MAX_MEDIA_DOWNLOAD_BYTES = 8 * 1024 * 1024


@dataclass
class _QueuedUpdate:
    update: Update
    context: ContextTypes.DEFAULT_TYPE


@dataclass
class _MessageBatch:
    items: list[_QueuedUpdate] = field(default_factory=list)
    future: asyncio.Future | None = None


_MESSAGE_BATCHES: dict[tuple[int, int], _MessageBatch] = {}
_MESSAGE_BATCHES_LOCK = asyncio.Lock()


def _consume_batch_future_exception(future: asyncio.Future) -> None:
    if future.cancelled():
        return
    try:
        future.exception()
    except Exception:
        return


def _cache_bot_identity(bot_user: telegram.User) -> None:
    """Cache bot identity globally and notify group history module."""
    global _BOT_ID, _BOT_USERNAME
    _BOT_ID = bot_user.id
    _BOT_USERNAME = bot_user.username or "FogMoeBot"
    group_chat_history.set_bot_identity(_BOT_ID, _BOT_USERNAME)


async def _refresh_bot_identity(bot, *, source: str) -> bool:
    try:
        bot_user = await bot.get_me()
    except telegram.error.NetworkError as exc:
        logger.warning(
            "Unable to fetch bot identity during %s; will retry later: %r",
            source,
            exc,
        )
        return False
    _cache_bot_identity(bot_user)
    return True


async def post_init(application) -> None:
    db.set_main_loop(asyncio.get_running_loop())
    await _refresh_bot_identity(application.bot, source="post_init")


class RateLimiter:
    def __init__(self, max_calls: int, time_window: float):
        self.max_calls = max_calls
        self.time_window = time_window
        self.calls = deque()

    def consume(self) -> bool:
        now = time.time()
        while self.calls and now - self.calls[0] > self.time_window:
            self.calls.popleft()
        if len(self.calls) < self.max_calls:
            self.calls.append(now)
            return True
        return False


_classifier_allowance = RateLimiter(max_calls=10, time_window=60.0)


# 添加一个帮助函数来获取实际的消息对象
def get_effective_message(update: Update):
    """获取有效的消息对象，无论是普通消息还是编辑后的消息"""
    return update.message or update.edited_message


def _format_message_timestamp(value) -> str | None:
    if not value:
        return None
    if hasattr(value, "strftime"):
        return value.strftime('%Y-%m-%d %H:%M:%S')
    return str(value)


def _format_xml_attrs(attrs: list[tuple[str, str | None]]) -> str:
    return " ".join(
        f'{key}="{xml_escape(value)}"' for key, value in attrs if value
    )


def _format_xml_message(
    *,
    chat_type: str,
    chat_title: str | None,
    timestamp: str,
    user_name: str,
    message_text: str,
    message_id: str | int | None = None,
    edited: bool = False,
    edited_at: str | None = None,
    forward_type: str | None = None,
    forward_origin_timestamp: str | None = None,
    forward_user: str | None = None,
    forward_name: str | None = None,
    forward_chat: str | None = None,
    forward_message_id: str | None = None,
    forward_author_signature: str | None = None,
    reply_user: str | None = None,
    reply_text: str | None = None,
    reply_type: str | None = None,
    reply_caption: str | None = None,
    reply_summary: str | None = None,
    reply_emoji: str | None = None,
    media_type: str | None = None,
    media_description: str | None = None,
    media_emoji: str | None = None,
) -> str:
    attrs = [
        ("type", chat_type),
        ("timestamp", timestamp),
        ("user", f"@{user_name}"),
        ("message_id", str(message_id) if message_id is not None else None),
        ("edited", "true" if edited else None),
        ("edited_at", edited_at if edited else None),
    ]
    if chat_type in ("group", "supergroup") and chat_title:
        attrs.insert(1, ("title", chat_title))
    attr_text = format_metadata_attrs(attrs)
    lines = [f"<metadata {attr_text}>"]
    if forward_type:
        forward_attr_text = _format_xml_attrs(
            [
                ("type", forward_type),
                ("origin_timestamp", forward_origin_timestamp),
                ("user", forward_user),
                ("name", forward_name),
                ("chat", forward_chat),
                ("message_id", forward_message_id),
                ("author_signature", forward_author_signature),
            ]
        )
        lines.append(f"  <forward {forward_attr_text} />")
    if reply_type:
        reply_user_value = f"@{reply_user}" if reply_user else ""
        reply_attr_text = _format_xml_attrs(
            [
                ("user", reply_user_value),
                ("type", reply_type),
                ("emoji", reply_emoji),
            ]
        )
        lines.append(f"  <reply {reply_attr_text}>")
        if reply_text:
            lines.append(f"    <text>{xml_escape(reply_text)}</text>")
        if reply_caption:
            lines.append(f"    <caption>{xml_escape(reply_caption)}</caption>")
        if reply_summary:
            lines.append(f"    <summary>{xml_escape(reply_summary)}</summary>")
        lines.append("  </reply>")
    elif reply_user or reply_text:
        reply_user_value = f"@{reply_user}" if reply_user else ""
        reply_attr = f' user="{xml_escape(reply_user_value)}"' if reply_user_value else ""
        lines.append(f"  <reply{reply_attr}>{xml_escape(reply_text or '')}</reply>")
    if media_type:
        media_attrs = [("type", media_type)]
        if media_emoji:
            media_attrs.append(("emoji", media_emoji))
        media_attr_text = _format_xml_attrs(media_attrs)
        lines.append(f"  <media {media_attr_text}>")
        if media_description:
            lines.append(
                f"    <description>{xml_escape(media_description)}</description>"
            )
        lines.append("  </media>")
    lines.append("</metadata>")
    lines.append(f"<message>{xml_escape(message_text)}</message>")
    return "\n".join(lines)


def _media_mime_type(media_type: str, effective_message) -> str | None:
    if media_type == "photo":
        return "image/jpeg"
    if media_type == "sticker":
        sticker = effective_message.sticker
        if getattr(sticker, "is_animated", False) or getattr(sticker, "is_video", False):
            return None
        return "image/webp"
    return None


def _build_reply_format_kwargs(reply_message) -> dict[str, str | None]:
    description = describe_message_for_context(reply_message)
    quoted_user = (
        getattr(getattr(reply_message, "from_user", None), "username", None)
        or "EmptyUsername"
    )

    if description.get("type") == "text":
        return {
            "reply_user": quoted_user,
            "reply_text": description.get("text") or "",
        }

    return {
        "reply_user": quoted_user,
        "reply_type": description.get("type") or "other",
        "reply_caption": description.get("caption"),
        "reply_summary": description.get("summary"),
        "reply_emoji": description.get("emoji"),
    }


def _build_forward_format_kwargs(message) -> dict[str, str | None]:
    description = describe_forward_for_context(message)
    if not description:
        return {}
    return {
        "forward_type": description.get("type"),
        "forward_origin_timestamp": description.get("origin_timestamp"),
        "forward_user": description.get("user"),
        "forward_name": description.get("name"),
        "forward_chat": description.get("chat"),
        "forward_message_id": description.get("message_id"),
        "forward_author_signature": description.get("author_signature"),
    }


def _build_multimodal_user_message(
    formatted_message: str,
    *,
    base64_str: str,
    mime_type: str | None,
) -> dict | None:
    if not mime_type:
        return None
    return {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": formatted_message,
            },
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime_type};base64,{base64_str}",
                },
            },
        ],
    }


def _replace_user_messages_for_ai(
    messages: list,
    replacements: list[tuple[str, dict]],
) -> list:
    if not replacements:
        return list(messages)

    messages_for_ai = list(messages)
    search_end = len(messages_for_ai) - 1
    for persisted_content, runtime_message in reversed(replacements):
        for index in range(search_end, -1, -1):
            message = messages_for_ai[index]
            if not isinstance(message, dict):
                continue
            if (
                message.get("role") == "user"
                and message.get("content") == persisted_content
            ):
                messages_for_ai[index] = runtime_message
                search_end = index - 1
                break

    return messages_for_ai


async def should_trigger_ai_response(message_text: str) -> bool:
    """
    使用配置的 classifier AI 模型判断群聊消息是否需要调用主 AI 回复。
    仅返回布尔结果，出现异常时默认不触发回复。
    """
    if not message_text:
        return False

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: _sync_should_trigger_ai_response(message_text)
    )


def _sync_should_trigger_ai_response(message_text: str) -> bool:
    if not _classifier_allowance.consume():
        logging.debug("AI classifier rate limiter blocked a request.")
        return False
    try:
        response = run_ai_task(
            "classifier",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是一个简洁的分类器。判断给定消息是否需要雾萌娘机器人主动回复。"
                        "仅在遇到相关问题必要时才回复，例如和AI聊天、寻求帮助、提问或请求信息等。"
                        "如果需要回复，请只回答 YES；如果不需要，请只回答 NO。"
                        "不要输出任何额外解释。"
                    ),
                },
                {
                    "role": "user",
                    "content": message_text,
                },
            ],
        )
        content = response.choices[0].message.content.strip().lower()
        return content.startswith("yes") or content.startswith("是")
    except Exception as exc:
        logging.error("AI 检测是否应回复失败: %s", exc)
        return False


def _message_trigger_text(message) -> str:
    parts = []
    text = getattr(message, "text", None)
    caption = getattr(message, "caption", None)
    if text:
        parts.append(str(text))
    if caption:
        parts.append(str(caption))
    return "\n".join(parts)


def _direct_trigger_phrases() -> list[str]:
    trigger_phrases = [
        str(trigger).strip().lower()
        for trigger in config.AI_DIRECT_TRIGGER_PHRASES
        if str(trigger).strip()
    ]
    bot_username = (_BOT_USERNAME or "FogMoeBot").strip().lower()
    if bot_username:
        trigger_phrases.append(f"@{bot_username}")
    return trigger_phrases


def _message_contains_direct_ai_trigger(message) -> bool:
    message_text = _message_trigger_text(message)
    if not message_text:
        return False

    normalized_text = message_text.lower()
    return any(trigger in normalized_text for trigger in _direct_trigger_phrases())


def _message_batch_key(update: Update) -> tuple[int, int] | None:
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return None
    return (chat.id, user.id)


def _batch_item_sort_key(item_and_message) -> tuple[float, int, int]:
    item, message = item_and_message
    message_date = getattr(message, "date", None)
    timestamp = message_date.timestamp() if message_date else 0.0
    message_id = getattr(message, "message_id", 0) or 0
    update_id = getattr(item.update, "update_id", 0) or 0
    return (timestamp, message_id, update_id)


async def reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    batch_key = _message_batch_key(update)
    if not batch_key:
        await _reply_unlocked(update, context)
        return
    if config.CHAT_BATCH_WINDOW_SECONDS <= 0:
        async with get_conversation_lock(batch_key[1]):
            await _reply_unlocked(update, context)
        return

    loop = asyncio.get_running_loop()
    is_owner = False
    async with _MESSAGE_BATCHES_LOCK:
        batch = _MESSAGE_BATCHES.get(batch_key)
        if batch is None:
            future = loop.create_future()
            future.add_done_callback(_consume_batch_future_exception)
            batch = _MessageBatch(future=future)
            _MESSAGE_BATCHES[batch_key] = batch
            is_owner = True
        batch.items.append(_QueuedUpdate(update=update, context=context))
        future = batch.future

    if is_owner:
        ready_batch = None
        try:
            await asyncio.sleep(config.CHAT_BATCH_WINDOW_SECONDS)
            async with _MESSAGE_BATCHES_LOCK:
                ready_batch = _MESSAGE_BATCHES.pop(batch_key, batch)

            async with get_conversation_lock(batch_key[1]):
                await _reply_batch_unlocked(ready_batch.items)

            if future and not future.done():
                future.set_result(None)
        except BaseException as exc:
            if future and not future.done():
                future.set_exception(exc)
            raise
        finally:
            async with _MESSAGE_BATCHES_LOCK:
                if _MESSAGE_BATCHES.get(batch_key) is batch:
                    _MESSAGE_BATCHES.pop(batch_key, None)
        return

    if future:
        await asyncio.shield(future)


async def _reply_unlocked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply_batch_unlocked([_QueuedUpdate(update=update, context=context)])


async def _reply_batch_unlocked(batch_items: list[_QueuedUpdate]) -> None:
    if not batch_items:
        return

    valid_items = []
    for item in batch_items:
        message = get_effective_message(item.update)
        if not message:
            logging.warning("收到无效的消息更新，忽略处理")
            continue
        valid_items.append((item, message))

    if not valid_items:
        return
    valid_items.sort(key=_batch_item_sort_key)

    update = valid_items[-1][0].update
    context = valid_items[-1][0].context
    effective_message = valid_items[-1][1]
    if not effective_message:
        logging.warning("收到无效的消息更新，忽略处理")
        return

    # 如果聊天是群组，则只对包含触发词时进行回复，
    if update.effective_chat.type in ("group", "supergroup"):
        if _BOT_ID is None:
            await _refresh_bot_identity(context.bot, source="group message handling")
        # 记录群聊上下文
        should_process_group_batch = False
        for _, message in valid_items:
            await group_chat_history.log_group_message(message, update.effective_chat.id)
            reply_from_user = getattr(
                getattr(message.reply_to_message, "from_user", None),
                "id",
                None,
            )
            if (
                message.reply_to_message
                and _BOT_ID is not None
                and reply_from_user == _BOT_ID
            ):
                should_process_group_batch = True
                continue

            if _message_contains_direct_ai_trigger(message):
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
        summary_text = await summary.generate_summary_immediately(conversation_id)
        if summary_text:
            await mysql_connection.async_update_latest_history_state_summary(
                conversation_id,
                summary_text,
            )
        else:
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
        current_message_time = _format_message_timestamp(message.date) or time.strftime(
            '%Y-%m-%d %H:%M:%S'
        )
        is_edited = bool(job.get("is_edited"))
        message_metadata_kwargs = {
            "message_id": getattr(message, "message_id", None),
            "edited": is_edited,
            "edited_at": (
                _format_message_timestamp(getattr(message, "edit_date", None))
                if is_edited
                else None
            ),
        }
        forward_kwargs = _build_forward_format_kwargs(message)
        reply_kwargs = (
            _build_reply_format_kwargs(message.reply_to_message)
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
                runtime_user_message = _build_multimodal_user_message(
                    runtime_formatted_message,
                    base64_str=base64_str,
                    mime_type=_media_mime_type(media_type, message),
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

        user_record_entries.append(("user", formatted_message))

    if not user_record_entries:
        return

    # 异步插入用户消息
    user_snapshot_created, user_storage_warning, user_archived_records = await mysql_connection.async_insert_chat_records(
        conversation_id,
        user_record_entries,
        system_prompt_extra=user_state_prompt,
    )
    if user_archived_records:
        await send_permanent_records_archive(
            context.bot,
            user_id,
            user_archived_records,
            logger=logger,
        )
    if user_storage_warning:
        remember_history_warning(user_storage_warning)
    await handle_overflow_summary(user_storage_warning)
    if user_snapshot_created and user_storage_warning != "overflow":
        summary.schedule_summary_generation(conversation_id)

    # 立即获取最新历史记录，以便AI能看到刚刚插入的消息
    chat_history = await mysql_connection.async_get_chat_history(conversation_id)

    chat_history_for_ai = _replace_user_messages_for_ai(
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
        "message_id": getattr(effective_message, "message_id", None),
        "user_id": user_id,
        "user_state_prompt": user_state_prompt,
    }
    sent_messages = []
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

    assistant_message, tool_logs = await ai_chat.get_ai_response(
        chat_history_for_ai,
        user_id,
        tool_context=tool_context,
        text_fallback_messages=chat_history,
        visible_content_handler=visible_content_handler,
    )
    sent_messages.extend(visible_content_handler.sent_messages)
    assistant_message = normalize_ai_reply_text(assistant_message)
    if assistant_message.strip():
        assistant_message = await normalize_sticker_directives(
            assistant_message,
            logger=logger,
        )

    tool_record_entries = tool_logs_to_record_entries(tool_logs)

    if tool_record_entries:
        tool_snapshot_created, tool_storage_warning, tool_archived_records = await mysql_connection.async_insert_chat_records(
            conversation_id,
            tool_record_entries,
        )
        if tool_archived_records:
            await send_permanent_records_archive(
                context.bot,
                user_id,
                tool_archived_records,
                logger=logger,
            )
        if tool_storage_warning:
            remember_history_warning(tool_storage_warning)
        await handle_overflow_summary(tool_storage_warning)
        if tool_snapshot_created and tool_storage_warning != "overflow":
            summary.schedule_summary_generation(conversation_id)

    if assistant_message.strip():
        # 异步插入AI回复到聊天记录
        (
            assistant_snapshot_created,
            assistant_storage_warning,
            assistant_archived_records,
        ) = await mysql_connection.async_insert_chat_record(
            conversation_id,
            "assistant",
            assistant_message,
        )
        if assistant_archived_records:
            await send_permanent_records_archive(
                context.bot,
                user_id,
                assistant_archived_records,
                logger=logger,
            )
        if assistant_storage_warning:
            remember_history_warning(assistant_storage_warning)
        await handle_overflow_summary(assistant_storage_warning)
        if assistant_snapshot_created and assistant_storage_warning != "overflow":
            summary.schedule_summary_generation(conversation_id)

    if pending_history_warning:
        await notify_history_warning(pending_history_warning)

    # 发送未通过可见循环即时发送的最终回复
    if assistant_message.strip():
        has_visible_message = bool(sent_messages)
        try:
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        except Exception:
            logger.debug("Failed to send typing action before final AI reply")
        sent_messages.extend(
            await send_ai_reply_with_stickers(
                bot=context.bot,
                chat_id=update.effective_chat.id,
                text=assistant_message,
                first_text_send=fallback_send if has_visible_message else effective_message.reply_text,
                fallback_send=fallback_send,
                logger=logger,
                reply_to_message_id=None if has_visible_message else getattr(effective_message, "message_id", None),
            )
        )
    sent_messages.extend(
        await send_generated_audio_from_tool_logs(
            bot=context.bot,
            chat_id=update.effective_chat.id,
            tool_logs=tool_logs,
            logger=logger,
        )
    )
    sent_messages.extend(
        await send_generated_images_from_tool_logs(
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
            await group_chat_history.log_group_message(sent_message, update.effective_chat.id)
