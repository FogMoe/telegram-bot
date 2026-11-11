"""AI 回复的整理与出站发送，用户消息、定时任务和空闲跟进三个入口共用。

- `finish_reply`：去掉不回复标记、根据结构化 `LoopStatus`（无状态时兼容固定错误文案）判定运行时错误、校验贴纸指令；
- `send_private_reply`：定时任务与空闲跟进把回复发到私聊；
- `send_generated_media`：发送本轮工具产出的媒体，三处共用同一个抑制历史记录的作用域。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fogmoe_telegram_bot.core.telegram_history import (
    suppress_telegram_history,
    telegram_history_scope,
)
from fogmoe_telegram_bot.core.telegram_utils import partial_send

from .reply_filter import normalize_ai_reply_text
from .sticker_sender import send_ai_reply_with_stickers
from .tools.specs import iter_media_tool_specs
from .types import LoopStatus, ToolLog


@dataclass(frozen=True, slots=True)
class AIReply:
    """模型阶段的最终回复。`runtime_error` 不为 None 时 `text` 是固定的错误提示，不当作 AI 回复写进历史。"""

    text: str
    runtime_error: str | None


async def finish_reply(
    text: str,
    *,
    normalize_stickers: Callable[[str], Awaitable[str]],
    status: LoopStatus,
) -> AIReply:
    reply_text = normalize_ai_reply_text(text)
    if reply_text.strip():
        reply_text = await normalize_stickers(reply_text)
    return AIReply(text=reply_text, runtime_error=status.runtime_error_cause())


async def send_private_reply(
    *,
    bot: Any,
    user_id: int,
    text: str,
    logger: logging.Logger,
    runtime_error: str | None = None,
) -> list[Any]:
    """把回复发到用户的私聊，返回已发出的消息；回复为空时什么都不发。

    固定的错误提示按错误通知的作用域记进历史；正常回复由调用方写历史，发送时不重复记录。
    """
    if not text.strip():
        return []
    try:
        await bot.send_chat_action(chat_id=user_id, action="typing")
    except Exception:
        logger.debug("Failed to send typing action before AI reply: user_id=%s", user_id)
    send_func = partial_send(bot.send_message, user_id)
    send_scope = (
        telegram_history_scope(
            user_id=user_id,
            chat_id=user_id,
            chat_type="private",
            origin="bot_runtime",
            event="error_notice",
            cause=runtime_error,
        )
        if runtime_error
        else suppress_telegram_history()
    )
    with send_scope:
        return await send_ai_reply_with_stickers(
            bot=bot,
            chat_id=user_id,
            text=text,
            first_text_send=send_func,
            fallback_send=send_func,
            logger=logger,
        )


async def send_generated_media(
    *,
    bot: Any,
    chat_id: int,
    tool_logs: list[ToolLog],
    logger: logging.Logger,
) -> list[Any]:
    """发送本轮工具产出的语音与图片，返回已发出的消息。

    这些消息由工具产生而非用户可见对话，因此写入期间抑制历史记录。
    """

    sent_messages: list[Any] = []
    with suppress_telegram_history():
        for _tool_name, media_spec in iter_media_tool_specs():
            sent_messages.extend(
                await media_spec.send_from_logs(
                    bot=bot,
                    chat_id=chat_id,
                    tool_logs=tool_logs,
                    logger=logger,
                )
            )
    return sent_messages
