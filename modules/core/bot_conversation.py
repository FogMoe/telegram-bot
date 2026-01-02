import asyncio
import base64
import json
import logging
import os
import tempfile
import time
from collections import deque

import telegram
from telegram import Update
from telegram.ext import ContextTypes

from core import config, db, group_chat_history, mysql_connection, process_user
from core.archive_utils import send_permanent_records_archive
from core.prompt_utils import format_metadata_attrs, format_user_state_prompt, xml_escape
from core.telegram_utils import partial_send, safe_send_markdown, split_ai_reply
from features.ai import ai_chat, summary

logger = logging.getLogger(__name__)


_BOT_ID: int | None = None
_BOT_USERNAME: str = "FogMoeBot"


def _cache_bot_identity(bot_user: telegram.User) -> None:
    """Cache bot identity globally and notify group history module."""
    global _BOT_ID, _BOT_USERNAME
    _BOT_ID = bot_user.id
    _BOT_USERNAME = bot_user.username or "FogMoeBot"
    group_chat_history.set_bot_identity(_BOT_ID, _BOT_USERNAME)


async def post_init(application) -> None:
    db.set_main_loop(asyncio.get_running_loop())
    bot_user = await application.bot.get_me()
    _cache_bot_identity(bot_user)


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


_zai_allowance = RateLimiter(max_calls=10, time_window=60.0)


# 添加一个帮助函数来获取实际的消息对象
def get_effective_message(update: Update):
    """获取有效的消息对象，无论是普通消息还是编辑后的消息"""
    return update.message or update.edited_message


def _format_xml_message(
    *,
    chat_type: str,
    chat_title: str | None,
    timestamp: str,
    user_name: str,
    message_text: str,
    reply_user: str | None = None,
    reply_text: str | None = None,
    media_type: str | None = None,
    media_description: str | None = None,
    media_emoji: str | None = None,
) -> str:
    attrs = [
        ("type", chat_type),
        ("timestamp", timestamp),
        ("user", f"@{user_name}"),
    ]
    if chat_type in ("group", "supergroup") and chat_title:
        attrs.insert(1, ("title", chat_title))
    attr_text = format_metadata_attrs(attrs)
    lines = [f"<metadata {attr_text}>"]
    if reply_user or reply_text:
        reply_user_value = f"@{reply_user}" if reply_user else ""
        reply_attr = (
            f' user="{xml_escape(reply_user_value)}"'
            if reply_user_value
            else ""
        )
        lines.append(
            f"  <reply{reply_attr}>{xml_escape(reply_text or '')}</reply>"
        )
    if media_type:
        media_attrs = [("type", media_type)]
        if media_emoji:
            media_attrs.append(("emoji", media_emoji))
        media_attr_text = " ".join(
            f'{key}="{xml_escape(value)}"' for key, value in media_attrs if value
        )
        lines.append(f"  <media {media_attr_text}>")
        if media_description:
            lines.append(
                f"    <description>{xml_escape(media_description)}</description>"
            )
        lines.append("  </media>")
    lines.append("</metadata>")
    lines.append(f"<message>{xml_escape(message_text)}</message>")
    return "\n".join(lines)


async def should_trigger_ai_response(message_text: str) -> bool:
    """
    使用 Z.ai glm-4.5-flash 模型判断群聊消息是否需要调用主 AI 回复。
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
    if not _zai_allowance.consume():
        logging.debug("Z.ai rate limiter blocked a request.")
        return False
    client = ai_chat.create_zhipu_client()
    try:
        response = client.chat.completions.create(
            model=config.ZHIPU_MODEL,
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
            temperature=0.0,
        )
        content = response.choices[0].message.content.strip().lower()
        return content.startswith("yes") or content.startswith("是")
    except Exception as exc:
        logging.error("Z.ai 检测是否应回复失败: %s", exc)
        return False


async def reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # 使用帮助函数获取有效消息
    effective_message = get_effective_message(update)
    if not effective_message:
        logging.warning("收到无效的消息更新，忽略处理")
        return

    # 如果聊天是群组，则只对包含触发词时进行回复，
    if update.effective_chat.type in ("group", "supergroup"):
        if _BOT_ID is None:
            bot_user = await context.bot.get_me()
            _cache_bot_identity(bot_user)
        # 记录群聊上下文
        await group_chat_history.log_group_message(effective_message, update.effective_chat.id)
        # 如果消息是回复给机器人的，则直接处理
        if (
            effective_message.reply_to_message
            and _BOT_ID is not None
            and effective_message.reply_to_message.from_user.id == _BOT_ID
        ):
            pass
        else:
            text = effective_message.text if effective_message.text else ""
            if not text:
                return
            if not (
                "/fogmoebot" in text
                or "@FogMoeBot" in text
                or "雾萌" in text
                or "fog moe" in text.lower()
                or "萌娘" in text
                or "fogmoe" in text.lower()
            ):
                # should_respond = await should_trigger_ai_response(text)
                # if not should_respond:
                return

    # 添加：检查用户是否在聊天冷却期内
    from core.command_cooldown import check_chat_cooldown
    if not await check_chat_cooldown(update):
        return  # 用户在冷却期内，直接返回

    user_id = update.effective_user.id
    user_name = update.effective_user.username or "EmptyUsername"  # 提供默认值，防止None值导致格式化错误
    # 确保消息时间安全获取
    message_time = effective_message.date.strftime('%Y-%m-%d %H:%M:%S') if effective_message.date else time.strftime('%Y-%m-%d %H:%M:%S')
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

    # 如果是媒体消息（图片或贴纸），固定硬币消耗3
    if effective_message.photo or effective_message.sticker:
        coin_cost = 3
        is_media = True
    else:
        # 保留原本文字消息长度判断逻辑
        user_message = effective_message.text
        if not user_message:
            logging.warning("收到没有文本内容的消息，忽略处理")
            return
        if len(user_message) > 4096:
            await effective_message.reply_text("消息过长，无法处理。请缩短消息长度！\nThe message is too long to process. Please shorten the message.")
            return
        elif len(user_message) > 1000:
            coin_cost = 3
        elif len(user_message) > 500:
            coin_cost = 2
        else:
            coin_cost = 1
        is_media = False

    async with mysql_connection.transaction() as connection:
        row = await mysql_connection.fetch_one(
            "SELECT permission, coins, info FROM user WHERE id = %s",
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
        user_coins = row[1]
        user_info_raw = row[2] if len(row) > 2 else ""

        if user_coins < coin_cost:
            await effective_message.reply_text(
                f"您的硬币不足，无法与雾萌娘连接，需要{coin_cost}个硬币。试试通过 /lottery 抽奖吧！\n"
                f"You don't have enough coins (need {coin_cost}), I don't want to talk to you. "
                f"Try using /lottery to get some coins!")
            return

        await connection.exec_driver_sql(
            "UPDATE user SET coins = coins - %s WHERE id = %s",
            (coin_cost, user_id),
        )
        user_coins = max(user_coins - coin_cost, 0)

    user_affection = await process_user.async_get_user_affection(user_id)
    user_impression_raw = await process_user.async_get_user_impression(user_id)
    impression_display = (user_impression_raw or "").strip()
    if impression_display:
        impression_display = impression_display.replace("\r", " ").replace("\n", " ")
        if len(impression_display) > 500:
            impression_display = impression_display[:497] + "..."
    else:
        impression_display = "未记录"

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
        user_permission=user_permission,
        user_affection=user_affection,
        impression=impression_display,
        personal_info=personal_info_display,
        diary_exists=diary_exists,
    )

    chat_type = update.effective_chat.type or "private"
    group_title = (update.effective_chat.title or "").strip() if update.effective_chat else ""

    # 如果是媒体消息，进行下载、AI分析、格式化描述
    if is_media:
        try:
            if effective_message.photo:
                media_type = "photo"
                file = await effective_message.photo[-1].get_file()
                media_emoji = None
            else:
                media_type = "sticker"
                file = await effective_message.sticker.get_file()
                media_emoji = getattr(effective_message.sticker, "emoji", None)

            # 检查是否有文本说明
            caption = effective_message.caption if effective_message.caption else ""

            # 使用临时文件来存储
            with tempfile.NamedTemporaryFile(delete=False) as temp_file:
                file_path = temp_file.name
                await file.download_to_drive(file_path)

            # 读取文件转base64
            with open(file_path, 'rb') as f:
                file_bytes = f.read()
            base64_str = base64.b64encode(file_bytes).decode('utf-8')

            # 删除临时文件
            os.unlink(file_path)

            # 异步调用图像分析AI
            image_description = await ai_chat.analyze_image(base64_str)

            # 组合图片描述和用户文本说明
            message_text = caption if caption else f"[{media_type}]"
            formatted_message = _format_xml_message(
                chat_type=chat_type,
                chat_title=group_title or None,
                timestamp=message_time,
                user_name=user_name,
                message_text=message_text,
                media_type=media_type,
                media_description=image_description,
                media_emoji=media_emoji,
            )

        except Exception as e:
            logging.error(f"处理媒体消息时出错: {str(e)}")
            await effective_message.reply_text(
                "抱歉呢，雾萌娘暂时无法处理您发送的媒体，请稍后再试试看喵~\n"
                "Sorry, I'm having trouble processing your image/sticker right now. Please try again later, meow!")
            return
    else:
        # 保留原有文本处理逻辑，处理文本消息
        user_message = effective_message.text or ""
        if effective_message.reply_to_message:
            quoted_message = (
                effective_message.reply_to_message.text
                or effective_message.reply_to_message.caption
                or "[non-text message]"
            )
            quoted_user = (
                effective_message.reply_to_message.from_user.username
                or "EmptyUsername"
            )
            formatted_message = _format_xml_message(
                chat_type=chat_type,
                chat_title=group_title or None,
                timestamp=message_time,
                user_name=user_name,
                message_text=user_message,
                reply_user=quoted_user,
                reply_text=quoted_message,
            )
        else:
            formatted_message = _format_xml_message(
                chat_type=chat_type,
                chat_title=group_title or None,
                timestamp=message_time,
                user_name=user_name,
                message_text=user_message,
            )

    # 异步获取聊天历史
    chat_history = await mysql_connection.async_get_chat_history(conversation_id)

    # 异步插入用户消息
    user_snapshot_created, user_storage_warning, user_archived_records = await mysql_connection.async_insert_chat_record(
        conversation_id,
        "user",
        formatted_message,
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

    chat_history_for_ai = list(chat_history)

    # 异步发送"正在输入"状态
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    # 异步获取AI回复
    tool_context = {
        "is_group": update.effective_chat.type in ("group", "supergroup"),
        "group_id": update.effective_chat.id if update.effective_chat.type in ("group", "supergroup") else None,
        "message_id": getattr(effective_message, "message_id", None),
        "user_id": user_id,
        "user_state_prompt": user_state_prompt,
    }

    assistant_message, tool_logs = await ai_chat.get_ai_response(chat_history_for_ai, user_id, tool_context=tool_context)

    pending_tool_call_ids = []
    for tool_log in tool_logs:
        entry_type = tool_log.get("type", "tool_result")
        tool_call_id = tool_log.get("tool_call_id")
        if not tool_call_id:
            if entry_type == "tool_result" and pending_tool_call_ids:
                tool_call_id = pending_tool_call_ids.pop(0)
            else:
                tool_call_id = f"auto_{int(time.time() * 1000)}"
        if entry_type == "assistant_tool_call":
            pending_tool_call_ids.append(tool_call_id)
            tool_log["tool_call_id"] = tool_call_id

        if entry_type == "assistant_tool_call":
            arguments = tool_log.get("arguments") or {}
            try:
                arguments_json = json.dumps(arguments, ensure_ascii=False)
            except TypeError:
                arguments_json = json.dumps({}, ensure_ascii=False)

            assistant_call_message = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": tool_call_id,
                        "type": "function",
                        "function": {
                            "name": tool_log.get("tool_name"),
                            "arguments": arguments_json,
                        },
                    }
                ],
            }

            tool_snapshot_created, tool_storage_warning, tool_archived_records = await mysql_connection.async_insert_chat_record(
                conversation_id,
                "assistant",
                assistant_call_message,
            )
        else:
            if pending_tool_call_ids and pending_tool_call_ids[0] == tool_call_id:
                pending_tool_call_ids.pop(0)
            tool_result = tool_log.get("result")
            try:
                tool_result_str = json.dumps(tool_result, ensure_ascii=False, default=str)
            except TypeError:
                tool_result_str = str(tool_result)

            tool_message = {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "name": tool_log.get("tool_name"),
                "content": tool_result_str,
            }

            tool_snapshot_created, tool_storage_warning, tool_archived_records = await mysql_connection.async_insert_chat_record(
                conversation_id,
                "tool",
                tool_message,
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

    # 异步插入AI回复到聊天记录
    (
        assistant_snapshot_created,
        assistant_storage_warning,
        assistant_archived_records,
    ) = await mysql_connection.async_insert_chat_record(conversation_id, "assistant", assistant_message)
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

    # 发送AI回复
    sent_messages = []
    fallback_send = partial_send(
        context.bot.send_message,
        update.effective_chat.id,
    )
    for index, segment in enumerate(split_ai_reply(assistant_message)):
        send_func = effective_message.reply_text if index == 0 else fallback_send
        results = await safe_send_markdown(
            send_func,
            segment,
            logger=logger,
            fallback_send=fallback_send,
        )
        sent_messages.extend(results)

    if update.effective_chat.type in ("group", "supergroup"):
        for sent_message in sent_messages:
            if sent_message is None:
                continue
            await group_chat_history.log_group_message(sent_message, update.effective_chat.id)
