"""
Utility functions and declarations for Gemini tool calling.

This module centralizes all tools that Gemini can invoke so ai_chat.py stays focused
on orchestration logic. Add new tool implementations and register them here.
"""

from typing import Callable, Dict, List, Optional
from contextvars import ContextVar

from google.genai import types

import config
import logging
import requests
import group_chat_history
import random
from datetime import datetime, timedelta, timezone

import mysql_connection
import process_user

SERPAPI_API_KEY = getattr(config, "SERPAPI_API_KEY", "")

_REQUEST_CONTEXT: ContextVar[Dict[str, object]] = ContextVar("tool_request_context", default={})


def set_tool_request_context(context: Optional[Dict[str, object]] = None) -> None:
    _REQUEST_CONTEXT.set(context or {})


def clear_tool_request_context() -> None:
    _REQUEST_CONTEXT.set({})


def _get_tool_request_context() -> Dict[str, object]:
    return _REQUEST_CONTEXT.get()


def get_help_text_tool() -> dict:
    """Return the configured help command list for the bot."""
    return {"help_text": config.HELP_TEXT}


def google_search_tool(query: str) -> dict:
    """Perform a Google search via SerpApi."""
    if not SERPAPI_API_KEY:
        return {"error": "SerpApi key is not configured."}

    params = {
        "engine": "google_light",
        "q": query,
        "api_key": SERPAPI_API_KEY,
    }

    try:
        response = requests.get("https://serpapi.com/search", params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        logging.exception("SerpApi request failed: %s", exc)
        return {"error": f"SerpApi request failed: {exc}"}

    return {
        "search_metadata": data.get("search_metadata", {}),
        "search_parameters": data.get("search_parameters", {}),
        "organic_results": data.get("organic_results", []) or [],
    }


def fetch_group_context_tool(
    window_size: int = 5,
    **kwargs,
) -> dict:
    """Retrieve recent messages before the current group chat message."""
    context = _get_tool_request_context()
    if not context.get("is_group"):
        return {"error": "当前对话不是群聊，无法获取上下文。"}

    target_group_id = context.get("group_id")
    if not target_group_id:
        return {"error": "缺少群聊标识，无法获取上下文。"}

    current_message_id = context.get("message_id")

    try:
        window_size = max(1, min(int(window_size), 100))
    except (TypeError, ValueError):
        window_size = 5

    around_message_id = current_message_id

    context_messages = group_chat_history.get_group_context(
        target_group_id,
        around_message_id,
        window_size,
    )
    return {
        "group_id": target_group_id,
        "around_message_id": around_message_id,
        "window_size": window_size,
        "messages": context_messages,
    }


def _get_user_by_username(username: str) -> Optional[Dict[str, object]]:
    connection = mysql_connection.create_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT id, name, coins FROM user WHERE name = %s",
            (username,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return {"id": row[0], "name": row[1], "coins": row[2]}
    finally:
        cursor.close()
        connection.close()


def _get_last_kindness(donor_id: int) -> Optional[Dict[str, object]]:
    connection = mysql_connection.create_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT amount, created_at FROM kindness_gifts "
            "WHERE donor_id = %s ORDER BY created_at DESC LIMIT 1",
            (donor_id,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return {"amount": row[0], "created_at": row[1]}
    finally:
        cursor.close()
        connection.close()


def kindness_gift_tool(
    recipient_username: str,
    amount: Optional[int] = None,
    **kwargs,
) -> dict:
    context = _get_tool_request_context()
    donor_id = context.get("user_id")
    if not donor_id:
        return {"error": "缺少赠送者身份信息，无法执行赠礼。"}

    username = (recipient_username or "").strip().lstrip("@")
    if not username:
        return {"error": "请输入有效的用户名。"}

    recipient = _get_user_by_username(username)
    if not recipient:
        return {"error": f"未找到用户名为 @{username} 的用户。"}

    last_record = _get_last_kindness(donor_id)
    if last_record and last_record.get("created_at"):
        last_time = last_record["created_at"]
        if last_time.tzinfo is None:
            last_time = last_time.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - last_time < timedelta(hours=24):
            return {
                "status": "cooldown",
                "last_amount": last_record["amount"],
                "last_time": last_time.isoformat(sep=" "),
                "message": "失败！今天已经使用过雾萌娘的仁慈，请24小时后再试。",
            }

    try:
        amt = int(amount) if amount is not None else random.randint(1, 10)
    except (TypeError, ValueError):
        amt = random.randint(1, 10)
    amt = max(1, min(amt, 10))

    connection = mysql_connection.create_connection()
    cursor = connection.cursor()
    try:
        process_user.update_user_coins(recipient["id"], amt)
        cursor.execute(
            "INSERT INTO kindness_gifts (donor_id, recipient_id, amount, created_at) "
            "VALUES (%s, %s, %s, NOW())",
            (donor_id, recipient["id"], amt),
        )
        connection.commit()
    except Exception as exc:
        connection.rollback()
        logging.error("Failed to record kindness gift: %s", exc)
        return {"error": "记录赠礼时出现问题，请稍后再试。"}
    finally:
        cursor.close()
        connection.close()

    latest = _get_last_kindness(donor_id)
    last_time_str = None
    last_amount = None
    if latest and latest.get("created_at"):
        lt = latest["created_at"]
        if lt.tzinfo is None:
            lt = lt.replace(tzinfo=timezone.utc)
        last_time_str = lt.isoformat(sep=" ")
        last_amount = latest.get("amount")

    return {
        "status": "granted",
        "donor_id": donor_id,
        "recipient_id": recipient["id"],
        "recipient_username": f"@{username}",
        "amount": amt,
        "last_time": last_time_str,
        "last_amount": last_amount,
        "recipient_coins_before": recipient["coins"],
        "recipient_coins_after": recipient["coins"] + amt,
        "message": f"已赠送 {amt} 枚金币给 @{username}，愿仁慈常伴。",
    }


def update_affection_tool(delta: int, **kwargs) -> dict:
    """调整AI对当前用户的好感度。"""
    context = _get_tool_request_context()
    user_id = context.get("user_id")
    if not user_id:
        return {"error": "缺少用户信息，无法更新好感度。"}

    try:
        change = int(delta)
    except (TypeError, ValueError):
        return {"error": "好感度变化值必须是整数。"}

    if change > 10:
        change = 10
    elif change < -10:
        change = -10

    try:
        affection = process_user.update_user_affection(user_id, change)
    except Exception as exc:
        logging.exception("Failed to update affection: %s", exc)
        return {"error": "更新好感度时出现错误，请稍后再试。"}

    return {
        "user_id": user_id,
        "change": change,
        "affection": affection,
        "message": f"好感度已调整 {change:+d}，当前值为 {affection}。",
    }


def update_impression_tool(impression: str, **kwargs) -> dict:
    """写入或覆盖AI对当前用户的印象文本。"""
    context = _get_tool_request_context()
    user_id = context.get("user_id")
    if not user_id:
        return {"error": "缺少用户信息，无法更新印象。"}

    text = (impression or "").strip()
    if len(text) > 500:
        text = text[:500]

    try:
        saved = process_user.update_user_impression(user_id, text)
    except Exception as exc:
        logging.exception("Failed to update impression: %s", exc)
        return {"error": "更新印象时出现错误，请稍后再试。"}

    return {
        "user_id": user_id,
        "impression": saved,
        "message": "印象记录已更新。",
    }


def fetch_permanent_summaries_tool(start: Optional[int] = None, end: Optional[int] = None, **kwargs) -> dict:
    """检索当前用户的永久对话摘要。"""
    context = _get_tool_request_context()
    user_id = context.get("user_id")
    if not user_id:
        return {"error": "缺少用户信息，无法检索摘要。"}

    try:
        start_idx = int(start) if start is not None else 1
    except (TypeError, ValueError):
        start_idx = 1

    try:
        end_idx = int(end) if end is not None else start_idx + 9
    except (TypeError, ValueError):
        end_idx = start_idx + 9

    if start_idx < 1:
        start_idx = 1
    if end_idx < start_idx:
        end_idx = start_idx

    window_size = end_idx - start_idx + 1
    window_size = max(1, min(window_size, 10))
    offset = start_idx - 1

    connection = mysql_connection.create_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT COUNT(*) FROM permanent_chat_records WHERE user_id = %s AND summary IS NOT NULL AND summary != ''",
            (user_id,),
        )
        total_rows = cursor.fetchone()[0] or 0

        cursor.execute(
            """
            SELECT id, summary, created_at
            FROM permanent_chat_records
            WHERE user_id = %s AND summary IS NOT NULL AND summary != ''
            ORDER BY created_at DESC, id DESC
            LIMIT %s OFFSET %s
            """,
            (user_id, window_size, offset),
        )
        rows = cursor.fetchall()
    finally:
        cursor.close()
        connection.close()

    records = []
    for row in rows:
        record_id, summary_text, created_at = row
        records.append(
            {
                "record_id": record_id,
                "created_at": created_at.isoformat(sep=" ") if created_at else None,
                "summary": summary_text,
            }
        )

    return {
        "user_id": user_id,
        "total": total_rows,
        "range_start": start_idx,
        "range_end": start_idx + len(records) - 1 if records else start_idx - 1,
        "records": records,
    }


# Build tool handlers once definitions are available.
GEMINI_TOOL_HANDLERS: Dict[str, Callable[..., dict]] = {
    "get_help_text": get_help_text_tool,
    "google_search": google_search_tool,
    "fetch_group_context": fetch_group_context_tool,
    "kindness_gift": kindness_gift_tool,
    "update_affection": update_affection_tool,
    "update_impression": update_impression_tool,
    "fetch_permanent_summaries": fetch_permanent_summaries_tool,
}


GEMINI_FUNCTION_DECLARATIONS: List[types.FunctionDeclaration] = [
    types.FunctionDeclaration(
        name="get_help_text",
        description=(
    "查询机器人的功能列表和用户可用命令。"
    "返回预配置的帮助文本，包含所有可用命令（如 /lottery、/me）及其用法说明。"
    "此工具无需参数，始终返回完整的命令清单。"
),
        parameters=types.Schema(type=types.Type.OBJECT,
    properties={},
    description="无需参数"),
    ),
    types.FunctionDeclaration(
        name="google_search",
        description=(
    "通过 Api 执行 Google 搜索，获取实时网络信息。"
    "适用场景：用户询问最新新闻、实时数据（天气、股票）、超出知识截止日期的信息。"
    "返回搜索元数据和自然搜索结果列表（包含标题、链接、摘要）。"
),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "query": types.Schema(
                    type=types.Type.STRING,
                    description=(
            "搜索查询字符串。可以是关键词、短语或完整问题。"
            "示例：'北京今天天气'、'Python 3.15 新特性'、'2025年奥运会'"
        ),
                ),
            },
            required=["query"],
        ),
    ),
    types.FunctionDeclaration(
        name="fetch_group_context",
        description=(
    "获取当前群聊消息之前的历史对话记录。"
    "仅在群聊场景下可用，私聊中调用会返回错误。"
    "用于理解群聊上下文，例如用户说'刚才那个'、'之前提到的'时需要回顾历史消息。"
    "返回按时间倒序排列的消息列表（最新消息在前），每条包含用户名、内容和时间戳。"
),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "window_size": types.Schema(
                    type=types.Type.INTEGER,
                            description=(
            "要检索的历史消息数量（向过去方向）。"
            "默认值：5，范围：1-100。"
            "例如：window_size=5 表示获取当前消息之前的 5 条消息。"
        ),
                ),
            },
            required=[],
        ),
    ),
    types.FunctionDeclaration(
        name="kindness_gift",
        description=(
    "雾萌娘向指定用户赠送金币，表达好感和鼓励。"
    "赠送者：雾萌娘自己。"
    "金额：1-10 枚金币，可指定或自动决定。"
    "冷却限制：雾萌娘24小时内只能使用一次。"
    "失败情况：用户不存在、在冷却期内、数据库错误。"
    "返回：赠送状态、金额、接收者金币余额变化。"
),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "recipient_username": types.Schema(
                    type=types.Type.STRING,
                    description=(
            "接收金币的用户的 Telegram 用户名，不含 @ 符号。"
            "示例：'usertgname'（不是 '@usertgname'）"
        ),
                ),
                "amount": types.Schema(
                    type=types.Type.INTEGER,
                            description=(
            "赠送的金币数量，范围 1-10。"
            "如果不指定或超出范围，将自动在 1-10 之间随机决定。"
        ),
                ),
            },
            required=["recipient_username"],
        ),
    ),
    types.FunctionDeclaration(
        name="update_affection",
        description=(
    "调整雾萌娘对当前用户的好感度数值。"
    "好感度范围：-100（厌恶）到 100（喜欢），影响雾萌娘的语气和态度。"
    "单次变化限制：-10 到 +10，超出此范围会自动截断到边界值。"
    "触发时机：用户行为引起明显情绪变化时（夸奖、侮辱、礼貌、骚扰等）。"
    "返回：变化值、调整后的好感度总值。"
),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "delta": types.Schema(
                    type=types.Type.INTEGER,
                            description=(
            "好感度变化值，正数表示上升，负数表示下降。"
            "推荐范围示例："
            "- 普通礼貌/用户夸奖：+1 到 +10"
            "- 用户侮辱/用户骚扰：-1 到 -10"
        ),
                ),
            },
            required=["delta"],
        ),
    ),
    types.FunctionDeclaration(
        name="update_impression",
description=(
    "写入或完全覆盖雾萌娘对当前用户的长期印象记录。"
    "警告：此操作会覆盖现有印象，不是追加。只有在印象明显变化或需要清空时才调用。"
    "适用场景：用户自我介绍、表达喜好、透露重要信息（职业、地点、偏好等）。"
    "不适用：临时情绪（'今天好累'）、重复现有印象。"
    "格式：完整的一句话或一小段话，如'用户是程序员，喜欢Python，工作地在北京'。"
    "长度限制：500 字符，超出会自动截断。"
),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "impression": types.Schema(
                    type=types.Type.STRING,
                            description=(
            "新的印象文本，必须是完整且自包含的描述。"
            "示例："
            "- '用户是程序员，喜欢 Python 和 Rust，讨厌写文档，工作地在北京。'"
            "- '用户叫小明，喜欢打游戏，养了一只猫叫咪咪。'"
            "长度上限：500 字符。"
        ),
                ),
            },
            required=["impression"],
        ),
    ),
    types.FunctionDeclaration(
        name="fetch_permanent_summaries",
        description=(
    "检索雾萌娘与当前用户的历史对话摘要记录。"
    "摘要按时间倒序排列（最新的排在第1位）。"
    "用途：用户提到'上次'、'之前'的对话时，回顾历史互动背景。"
    "返回：摘要列表，每条包含 record_id、created_at（时间戳）、summary（摘要文本）。"
    "限制：单次最多返回 10 条摘要。"
),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "start": types.Schema(
                    type=types.Type.INTEGER,
                    description=(
            "起始位置（从 1 开始）。"
            "1 表示最新的摘要，2 表示第二新的摘要，以此类推。"
            "默认值：1（从最新开始）"
        ),
                ),
                "end": types.Schema(
                    type=types.Type.INTEGER,
                    description=(
            "结束位置（包含）。"
            "默认值：start + 9（即返回 10 条）。"
            "示例：start=1, end=5 返回最新的 5 条摘要。"
        ),
                ),
            },
            required=[],
        ),
    ),
]

__all__ = [
    "GEMINI_FUNCTION_DECLARATIONS",
    "GEMINI_TOOL_HANDLERS",
    "set_tool_request_context",
    "clear_tool_request_context",
    "kindness_gift_tool",
    "update_affection_tool",
    "update_impression_tool",
    "fetch_permanent_summaries_tool",
]
