import asyncio
import logging
from typing import Optional

from telegram import Bot
from telegram.error import TelegramError

from core import config, mysql_connection

from .context import get_tool_request_context

logger = logging.getLogger(__name__)

MAX_CHANNEL_READ = 50


def _run_bot_coroutine(coro):
    """在同步工具中执行 Telegram 异步调用。"""
    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:
        running_loop = None

    if running_loop and running_loop.is_running():
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()
    return asyncio.run(coro)


async def _with_bot(action):
    bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
    try:
        return await action(bot)
    finally:
        await bot.close()


def _get_bound_channel(user_id: int) -> Optional[dict]:
    row = mysql_connection.run_sync(
        mysql_connection.fetch_one(
            "SELECT channel_id, channel_title, channel_username FROM ai_user_channel_bindings WHERE user_id = %s",
            (user_id,),
        )
    )
    if not row:
        return None
    channel_id, channel_title, channel_username = row
    return {
        "channel_id": channel_id,
        "channel_title": channel_title,
        "channel_username": channel_username,
    }


def _format_channel_label(binding: dict) -> str:
    username = binding.get("channel_username")
    channel_id = binding.get("channel_id")
    if username:
        return f"@{username}"
    return str(channel_id) if channel_id is not None else ""


def _normalize_action(action: Optional[str]) -> Optional[str]:
    if not action:
        return None
    value = str(action).strip().lower()
    if value in {"read", "list", "show"}:
        return "read"
    if value in {"post", "publish", "send", "create"}:
        return "post"
    if value in {"edit", "update"}:
        return "edit"
    if value in {"delete", "remove"}:
        return "delete"
    return None


def _normalize_message_id(value: Optional[int | str]) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_limit(value: Optional[int | str]) -> int:
    try:
        limit = int(value) if value is not None else 10
    except (TypeError, ValueError):
        limit = 10
    return max(1, min(limit, MAX_CHANNEL_READ))


def _read_channel_posts(channel_id: int, limit: int) -> list[dict]:
    rows = mysql_connection.run_sync(
        mysql_connection.fetch_all(
            "SELECT message_id, message_date, message_type, text, caption, file_id "
            "FROM ai_channel_posts WHERE channel_id = %s "
            "ORDER BY message_date DESC, message_id DESC LIMIT %s",
            (channel_id, limit),
        )
    )
    records: list[dict] = []
    for row in rows:
        message_id, message_date, message_type, text, caption, file_id = row
        records.append(
            {
                "message_id": message_id,
                "message_date": message_date.isoformat(sep=" ") if message_date else None,
                "message_type": message_type,
                "text": text,
                "caption": caption,
                "file_id": file_id,
            }
        )
    return records


def channel_tool(
    action: Optional[str] = None,
    text: Optional[str] = None,
    message_id: Optional[int] = None,
    limit: Optional[int] = None,
    **kwargs,
) -> dict:
    """操作用户绑定频道的工具入口。"""
    request_context = get_tool_request_context()
    user_id = request_context.get("user_id")
    if not user_id:
        return {"error": "缺少用户信息，无法操作频道"}

    binding = _get_bound_channel(int(user_id))
    if not binding:
        return {"error": "未找到已绑定的频道，请先使用 /channel bind 进行绑定"}

    action_value = _normalize_action(action)
    if not action_value:
        return {"error": "未知操作，请使用 read/post/edit/delete"}

    channel_id = binding["channel_id"]
    channel_label = _format_channel_label(binding)

    if action_value == "read":
        read_limit = _normalize_limit(limit)
        records = _read_channel_posts(channel_id, read_limit)
        return {
            "status": "ok",
            "action": "read",
            "channel_id": channel_id,
            "channel": channel_label,
            "count": len(records),
            "records": records,
        }

    if action_value in {"post", "edit"}:
        payload_text = (text or "").strip()
        if not payload_text:
            return {"error": "缺少文本内容，无法发送或编辑"}

    if action_value == "post":
        if not config.TELEGRAM_BOT_TOKEN:
            return {"error": "Telegram Bot Token 未配置"}
        try:
            message = _run_bot_coroutine(
                _with_bot(lambda bot: bot.send_message(chat_id=channel_id, text=payload_text))
            )
        except TelegramError as exc:
            logger.warning("发布频道消息失败: %s", exc)
            return {"error": f"发布失败: {exc}"}
        return {
            "status": "ok",
            "action": "post",
            "channel_id": channel_id,
            "channel": channel_label,
            "message_id": getattr(message, "message_id", None),
        }

    if action_value == "edit":
        target_message_id = _normalize_message_id(message_id)
        if not target_message_id:
            return {"error": "缺少 message_id，无法编辑"}
        if not config.TELEGRAM_BOT_TOKEN:
            return {"error": "Telegram Bot Token 未配置"}
        try:
            message = _run_bot_coroutine(
                _with_bot(
                    lambda bot: bot.edit_message_text(
                        chat_id=channel_id,
                        message_id=target_message_id,
                        text=payload_text,
                    )
                )
            )
        except TelegramError as exc:
            logger.warning("编辑频道消息失败: %s", exc)
            return {"error": f"编辑失败: {exc}"}
        return {
            "status": "ok",
            "action": "edit",
            "channel_id": channel_id,
            "channel": channel_label,
            "message_id": getattr(message, "message_id", target_message_id),
        }

    if action_value == "delete":
        target_message_id = _normalize_message_id(message_id)
        if not target_message_id:
            return {"error": "缺少 message_id，无法删除"}
        if not config.TELEGRAM_BOT_TOKEN:
            return {"error": "Telegram Bot Token 未配置"}
        try:
            result = _run_bot_coroutine(
                _with_bot(lambda bot: bot.delete_message(chat_id=channel_id, message_id=target_message_id))
            )
        except TelegramError as exc:
            logger.warning("删除频道消息失败: %s", exc)
            return {"error": f"删除失败: {exc}"}
        return {
            "status": "ok",
            "action": "delete",
            "channel_id": channel_id,
            "channel": channel_label,
            "message_id": target_message_id,
            "deleted": bool(result) if result is not None else True,
        }

    return {"error": "未处理的操作"}
