"""一轮对话对外部世界的依赖。

`TurnServices` 的每个字段是一个可调用对象；`default_services()` 构造生产实现，按调用时的模块
属性解析（monkeypatch `ai_chat.get_ai_response` 之类的旧写法仍然有效）。测试用
`dataclasses.replace(default_services(), run_model=...)` 替换需要的字段，或整个自己构造。

字段按职责分组：

- 扣费：事务边界在 `billing.charge_turn` 内，见 `turn.py` 的「事务所有权」；
- 历史与用户状态：每个调用各自是一次短事务，没有跨调用的事务；
- 模型：`run_model` 是整轮里唯一执行模型（与工具循环）的调用点；
- 投递：只有 Telegram 与出站发送，不碰数据库事务。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from telegram import Bot, Message

from fogmoe_telegram_bot.core import (
    group_chat_history,
    mysql_connection,
    process_user,
    telegram_history,
)
from fogmoe_telegram_bot.core.archive_utils import send_permanent_records_archive
from fogmoe_telegram_bot.core.redaction import redact_text
from fogmoe_telegram_bot.core.telegram_utils import partial_send, safe_send_markdown
from fogmoe_telegram_bot.features.ai import ai_chat, idle_followup, outbound, sticker_sender, summary
from fogmoe_telegram_bot.features.ai.telegram_visible_sender import TelegramVisibleContentHandler

from . import billing, history_hooks
from .turn_types import HistoryInsert, ModelRequest, ModelResponse, UserStateRecord

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TurnServices:
    # 扣费
    charge: Callable[[int, Sequence[billing.TurnMessage]], Awaitable[billing.TurnCharge]]

    # 历史与用户状态
    flush_events: Callable[[int], Awaitable[None]]
    load_user_state: Callable[[int], Awaitable[UserStateRecord]]
    insert_records: Callable[..., Awaitable[HistoryInsert]]
    insert_record: Callable[..., Awaitable[HistoryInsert]]
    get_history: Callable[[int], Awaitable[list[dict[str, Any]]]]
    schedule_summary: Callable[[int], None]
    handle_history_overflow: Callable[[int], Awaitable[None]]
    arm_idle_followup: Callable[[int], Awaitable[None]]
    archive_completed_clear: Callable[..., Awaitable[None]]

    # 媒体
    analyze_image: Callable[[str], Awaitable[str]]

    # 模型
    run_model: Callable[[ModelRequest], Awaitable[ModelResponse]]
    make_visible_handler: Callable[..., Any]

    # 投递
    reply_text: Callable[[Message, str], Awaitable[Any]]
    send_typing: Callable[[Bot, int], Awaitable[Any]]
    send_warning: Callable[[Bot, int, str], Awaitable[Any]]
    send_archive: Callable[[Bot, int, list[Any]], Awaitable[Any]]
    normalize_stickers: Callable[[str], Awaitable[str]]
    send_reply: Callable[..., Awaitable[list[Any]]]
    send_generated_media: Callable[..., Awaitable[list[Any]]]
    log_group_message: Callable[[Any, int], Awaitable[None]]


async def _charge(
    user_id: int,
    messages: Sequence[billing.TurnMessage],
) -> billing.TurnCharge:
    return await billing.charge_turn(user_id, messages)


async def _flush_events(conversation_id: int) -> None:
    await telegram_history.flush_pending_events(conversation_id)


async def _load_user_state(user_id: int) -> UserStateRecord:
    impression = await process_user.async_get_user_impression(user_id)
    diary_row = await mysql_connection.fetch_one(
        "SELECT 1 FROM ai_user_diary_pages WHERE user_id = %s AND content != '' LIMIT 1",
        (user_id,),
    )
    return UserStateRecord(impression=impression, diary_exists=bool(diary_row))


async def _insert_records(
    conversation_id: int,
    entries: list[tuple[str, Any]],
    **options: Any,
) -> HistoryInsert:
    return await mysql_connection.async_insert_chat_records(
        conversation_id,
        entries,
        **options,
    )


async def _insert_record(
    conversation_id: int,
    role: str,
    content: str,
    **options: Any,
) -> HistoryInsert:
    return await mysql_connection.async_insert_chat_record(
        conversation_id,
        role,
        content,
        **options,
    )


async def _get_history(conversation_id: int) -> list[dict[str, Any]]:
    return await mysql_connection.async_get_chat_history(conversation_id)


def _schedule_summary(conversation_id: int) -> None:
    summary.schedule_summary_generation(conversation_id)


async def _handle_history_overflow(conversation_id: int) -> None:
    await history_hooks.handle_history_overflow(conversation_id)


async def _arm_idle_followup(user_id: int) -> None:
    await idle_followup.arm_from_private_turn(user_id)


async def _archive_completed_clear(
    *,
    bot: Bot,
    user_id: int,
    conversation_id: int,
    tool_record_entries: list[tuple[str, object]],
    assistant_message: str,
    runtime_error: str | None,
) -> None:
    """把 AI 代执行 /clear 的完整当前轮归档，并重置活跃会话。"""
    await telegram_history.flush_pending_events(conversation_id)
    clear_records = list(tool_record_entries)
    if assistant_message.strip() and not runtime_error:
        clear_records.append(("assistant", redact_text(assistant_message)))

    clear_record_id, clear_archived_records = (
        await mysql_connection.archive_chat_and_start_new_session(
            conversation_id,
            clear_records,
        )
    )

    archive_delivery_events: list[str] = []
    if clear_archived_records:
        with telegram_history.capture_telegram_history_events(user_id) as archive_delivery_events:
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


async def _analyze_image(base64_str: str) -> str:
    return await ai_chat.analyze_image(base64_str)


async def _run_model(request: ModelRequest) -> ModelResponse:
    text, tool_logs = await ai_chat.get_ai_response(
        request.messages,
        request.user_id,
        tool_context=request.tool_context,
        text_fallback_messages=request.text_fallback_messages,
        visible_content_handler=request.visible_content_handler,
        deadline=request.deadline,
    )
    return ModelResponse(text=text, tool_logs=tool_logs)


def _make_visible_handler(**kwargs: Any) -> TelegramVisibleContentHandler:
    return TelegramVisibleContentHandler(logger=logger, **kwargs)


async def _reply_text(message: Message, text: str) -> Any:
    return await message.reply_text(text)


async def _send_typing(bot: Bot, chat_id: int) -> Any:
    return await bot.send_chat_action(chat_id=chat_id, action="typing")


async def _send_warning(bot: Bot, chat_id: int, text: str) -> Any:
    return await safe_send_markdown(
        partial_send(bot.send_message, chat_id),
        text,
        logger=logger,
    )


async def _send_archive(bot: Bot, user_id: int, archived_records: list[Any]) -> Any:
    return await send_permanent_records_archive(
        bot,
        user_id,
        archived_records,
        logger=logger,
    )


async def _normalize_stickers(text: str) -> str:
    return await sticker_sender.normalize_sticker_directives(text, logger=logger)


async def _send_reply(**kwargs: Any) -> list[Any]:
    return await sticker_sender.send_ai_reply_with_stickers(logger=logger, **kwargs)


async def _send_generated_media(**kwargs: Any) -> list[Any]:
    return await outbound.send_generated_media(logger=logger, **kwargs)


async def _log_group_message(message: Any, chat_id: int) -> None:
    await group_chat_history.log_group_message(message, chat_id)


def default_services() -> TurnServices:
    """生产实现。每轮对话构造一次，因此测试对被引用模块的 monkeypatch 总能生效。"""
    return TurnServices(
        charge=_charge,
        flush_events=_flush_events,
        load_user_state=_load_user_state,
        insert_records=_insert_records,
        insert_record=_insert_record,
        get_history=_get_history,
        schedule_summary=_schedule_summary,
        handle_history_overflow=_handle_history_overflow,
        arm_idle_followup=_arm_idle_followup,
        archive_completed_clear=_archive_completed_clear,
        analyze_image=_analyze_image,
        run_model=_run_model,
        make_visible_handler=_make_visible_handler,
        reply_text=_reply_text,
        send_typing=_send_typing,
        send_warning=_send_warning,
        send_archive=_send_archive,
        normalize_stickers=_normalize_stickers,
        send_reply=_send_reply,
        send_generated_media=_send_generated_media,
        log_group_message=_log_group_message,
    )
