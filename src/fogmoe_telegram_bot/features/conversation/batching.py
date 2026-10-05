"""私聊消息的批处理窗口。

用户连发几条时先攒进同一个批次，等窗口关闭后交给一次 AI 轮次处理，
避免一条消息一次调用。
"""

import asyncio
import logging
from dataclasses import dataclass, field

from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


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
