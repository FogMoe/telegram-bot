"""游戏面板消息的尽力编辑。

面板编辑在状态已经提交之后进行，失败不能影响结算结果。`edit_panel` 区分两类失败：
Telegram 明确拒绝的（消息内容没变、消息已删除、机器人被拉黑）重试不会有结果，视为处理完毕；
网络抖动、超时、限流之类以后还可能成功，返回 False 让恢复任务稍后再试。
"""

from __future__ import annotations

import logging
from typing import Any

from telegram.error import BadRequest, Forbidden

logger = logging.getLogger(__name__)


async def edit_panel(
    bot: Any,
    *,
    chat_id: int,
    message_id: int | None,
    text: str,
    reply_markup: Any = None,
) -> bool:
    """编辑面板消息。返回 True 表示不需要再重试（成功，或 Telegram 明确拒绝）。"""
    if message_id is None:
        return True
    try:
        await bot.edit_message_text(
            text=text,
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=reply_markup,
        )
    except (BadRequest, Forbidden) as exc:
        logger.debug("面板消息无法编辑 chat=%s message=%s: %s", chat_id, message_id, exc)
        return True
    except Exception as exc:
        logger.warning("面板消息编辑失败，稍后重试 chat=%s message=%s: %s", chat_id, message_id, exc)
        return False
    return True
