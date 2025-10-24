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
        return {"error": "当前对话不是群聊，无法获取上下文"}

    target_group_id = context.get("group_id")
    if not target_group_id:
        return {"error": "缺少群聊标识，无法获取上下文"}

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
        return {"error": "缺少赠送者身份信息，无法执行赠礼"}

    username = (recipient_username or "").strip().lstrip("@")
    if not username:
        return {"error": "请输入有效的用户名"}

    recipient = _get_user_by_username(username)
    if not recipient:
        return {"error": f"未找到用户名为 @{username} 的用户"}

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
                "message": "失败，冷却时间24小时未到，无法再次赠送金币",
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
        return {"error": "记录赠礼时出现问题，请稍后再试"}
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
        "message": f"已成功赠送 {amt} 枚金币给 @{username}",
    }


def update_affection_tool(delta: int, **kwargs) -> dict:
    """调整AI对当前用户的好感度。"""
    context = _get_tool_request_context()
    user_id = context.get("user_id")
    if not user_id:
        return {"error": "缺少用户信息，无法更新好感度"}

    try:
        change = int(delta)
    except (TypeError, ValueError):
        return {"error": "好感度变化值必须是整数"}

    if change > 10:
        change = 10
    elif change < -10:
        change = -10

    try:
        affection = process_user.update_user_affection(user_id, change)
    except Exception as exc:
        logging.exception("Failed to update affection: %s", exc)
        return {"error": "更新好感度时出现错误，请稍后再试"}

    return {
        "user_id": user_id,
        "change": change,
        "affection": affection,
        "message": f"好感度已调整 {change:+d}，当前值为 {affection}",
    }


def update_impression_tool(impression: str, **kwargs) -> dict:
    """写入或覆盖AI对当前用户的印象文本。"""
    context = _get_tool_request_context()
    user_id = context.get("user_id")
    if not user_id:
        return {"error": "缺少用户信息，无法更新印象"}

    text = (impression or "").strip()
    if len(text) > 500:
        text = text[:500]

    try:
        saved = process_user.update_user_impression(user_id, text)
    except Exception as exc:
        logging.exception("Failed to update impression: %s", exc)
        return {"error": "更新印象时出现错误"}

    return {
        "user_id": user_id,
        "impression": saved,
        "message": "印象记录已更新",
    }


def fetch_permanent_summaries_tool(start: Optional[int] = None, end: Optional[int] = None, **kwargs) -> dict:
    """检索当前用户的永久对话摘要。"""
    context = _get_tool_request_context()
    user_id = context.get("user_id")
    if not user_id:
        return {"error": "缺少用户信息，无法检索摘要"}

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
        description=("Returns a list of available Telegram commands and features for users"),
        parameters=types.Schema(type=types.Type.OBJECT,
    properties={},
    description="No parameters required"),
    ),
    types.FunctionDeclaration(
        name="google_search",
        description=("Use Google search engine to obtain the latest information and answers"),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "query": types.Schema(
                    type=types.Type.STRING,
                    description=("Search query string. Can be keywords, phrases, or complete questions"),
                ),
            },
            required=["query"],
        ),
    ),
    types.FunctionDeclaration(
        name="fetch_group_context",
        description=("Fetch message history from group chat"),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "window_size": types.Schema(
                    type=types.Type.INTEGER,
                    description=(
                        "Number of historical messages to retrieve. Range: 1-100"
                    ),
                ),
            },
            required=[],
        ),
    ),
    types.FunctionDeclaration(
        name="kindness_gift",
        description=("Gift a certain amount of coins to the user based on your affection level towards them"),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "recipient_username": types.Schema(
                    type=types.Type.STRING,
                    description=("User's Telegram username, without the @ symbol"),
                ),
                "amount": types.Schema(
                    type=types.Type.INTEGER,
                            description=("Amount of coins to gift. Range: 1-10"),
                ),
            },
            required=["recipient_username"],
        ),
    ),
    types.FunctionDeclaration(
        name="update_affection",
        description=("Adjust your affection level towards the user"),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "delta": types.Schema(
                    type=types.Type.INTEGER,
                            description=("Affection level change value. Positive numbers indicate increase, negative numbers indicate decrease. Range: 1-10"),
                ),
            },
            required=["delta"],
        ),
    ),
    types.FunctionDeclaration(
        name="update_impression",
        description=("Update permanent impression of the user"),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "impression": types.Schema(
                    type=types.Type.STRING,
                            description=(
            "New impression text, complete and self-contained description"
        ),
                ),
            },
            required=["impression"],
        ),
    ),
    types.FunctionDeclaration(
        name="fetch_permanent_summaries",
        description=(
            "Fetch user's historical conversation summaries, returns up to 10 entries"
),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "start": types.Schema(
                    type=types.Type.INTEGER,
                    description=(
            "Start position"
        ),
                ),
                "end": types.Schema(
                    type=types.Type.INTEGER,
                    description=(
            "End position"
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
