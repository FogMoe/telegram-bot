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
    window_size: int = 10,
    **kwargs,
) -> dict:
    """Retrieve recent messages before the current group chat message."""
    context = _get_tool_request_context()
    if not context.get("is_group"):
        return {"error": "This is not a group chat, cannot fetch context"}

    target_group_id = context.get("group_id")
    if not target_group_id:
        return {"error": "Missing group chat identifier, cannot fetch context"}

    current_message_id = context.get("message_id")

    try:
        window_size = max(1, min(int(window_size), 100))
    except (TypeError, ValueError):
        window_size = 10

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


def _get_user_by_id(user_id: int) -> Optional[Dict[str, object]]:
    connection = mysql_connection.create_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT id, name, coins FROM user WHERE id = %s",
            (user_id,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return {"id": row[0], "name": row[1], "coins": row[2]}
    finally:
        cursor.close()
        connection.close()


def _get_last_kindness_for_recipient(recipient_id: int) -> Optional[Dict[str, object]]:
    connection = mysql_connection.create_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT amount, created_at FROM kindness_gifts "
            "WHERE recipient_id = %s ORDER BY created_at DESC LIMIT 1",
            (recipient_id,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return {"amount": row[0], "created_at": row[1]}
    finally:
        cursor.close()
        connection.close()


def kindness_gift_tool(
    amount: Optional[int] = None,
    **kwargs,
) -> dict:
    context = _get_tool_request_context()
    try:
        recipient_id = int(context.get("user_id"))
    except (TypeError, ValueError):
        return {"error": "Missing recipient information, cannot execute gift"}

    recipient = _get_user_by_id(recipient_id)
    if not recipient:
        return {"error": "Recipient user not found"}

    last_record = _get_last_kindness_for_recipient(recipient["id"])
    if last_record and last_record.get("created_at"):
        last_time = last_record["created_at"]
        if last_time.tzinfo is None:
            last_time = last_time.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - last_time < timedelta(hours=24):
            return {
                "status": "cooldown",
                "last_amount": last_record["amount"],
                "last_time": last_time.isoformat(sep=" "),
                "message": "Failed: 24-hour cooldown period has not elapsed. Cannot gift coins again yet",
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
            "INSERT INTO kindness_gifts (recipient_id, amount, created_at) "
            "VALUES (%s, %s, NOW())",
            (recipient["id"], amt),
        )
        connection.commit()
    except Exception as exc:
        connection.rollback()
        logging.error("Failed to record kindness gift: %s", exc)
        return {"error": "Error recording gift, please try again later"}
    finally:
        cursor.close()
        connection.close()

    latest = _get_last_kindness_for_recipient(recipient["id"])
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
        "recipient_id": recipient["id"],
        "recipient_username": f"@{recipient['name']}" if recipient.get("name") else None,
        "amount": amt,
        "last_time": last_time_str,
        "last_amount": last_amount,
        "recipient_coins_before": recipient["coins"],
        "recipient_coins_after": recipient["coins"] + amt,
        "message": f"Successfully gifted {amt} coins to user",
    }


def update_affection_tool(delta: int, **kwargs) -> dict:
    """调整AI对当前用户的好感度。"""
    context = _get_tool_request_context()
    user_id = context.get("user_id")
    if not user_id:
        return {"error": "Missing user information, cannot update affection level"}

    try:
        change = int(delta)
    except (TypeError, ValueError):
        return {"error": "Affection change value must be an integer"}

    if change > 10:
        change = 10
    elif change < -10:
        change = -10

    try:
        affection = process_user.get_user_affection(user_id)
    except Exception as exc:
        logging.exception("Failed to fetch affection: %s", exc)
        return {"error": "Error querying affection level, please try again later"}

    if affection is None:
        return {"error": "User affection data not found"}

    if (affection >= 100 and change > 0) or (affection <= -100 and change < 0):
        return {"error": "Affection level has reached the limit, cannot adjust further"}

    try:
        new_affection = process_user.update_user_affection(user_id, change)
    except Exception as exc:
        logging.exception("Failed to update affection: %s", exc)
        return {"error": "Error updating affection level, please try again later"}

    return {
        "user_id": user_id,
        "change": change,
        "affection": new_affection,
        "message": f"Affection level adjusted by {change:+d}, current value: {new_affection}",
    }


def update_impression_tool(impression: str, **kwargs) -> dict:
    """写入或覆盖AI对当前用户的印象文本。"""
    context = _get_tool_request_context()
    user_id = context.get("user_id")
    if not user_id:
        return {"error": "Missing user information, cannot update impression"}

    text = (impression or "").strip()
    if len(text) > 500:
        text = text[:500]

    try:
        saved = process_user.update_user_impression(user_id, text)
    except Exception as exc:
        logging.exception("Failed to update impression: %s", exc)
        return {"error": "Error updating impression"}

    return {
        "user_id": user_id,
        "impression": saved,
        "message": "Impression record updated successfully",
    }


def fetch_permanent_summaries_tool(start: Optional[int] = None, end: Optional[int] = None, **kwargs) -> dict:
    """检索当前用户的永久对话摘要。"""
    context = _get_tool_request_context()
    user_id = context.get("user_id")
    if not user_id:
        return {"error": "Missing user information, cannot retrieve summaries"}

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
        description=("Fetch message history from group chat (group chats only)"),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "window_size": types.Schema(
                    type=types.Type.INTEGER,
                    description=(
                        "Number of historical messages to retrieve"
                    ),
                    default=10,
                    minimum=1,
                    maximum=100
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
                "amount": types.Schema(
                    type=types.Type.INTEGER,
                    description=("Amount of coins to gift"),
                    minimum=1,
                    maximum=10
                ),
            },
            required=[],
        ),
    ),
    types.FunctionDeclaration(
        name="update_affection",
        description=("Adjust your affection level towards the user (range: -100 to 100)"),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "delta": types.Schema(
                    type=types.Type.INTEGER,
                            description=("Affection level change value. Positive numbers indicate increase, negative numbers indicate decrease"),
                            minimum=-10,
                            maximum=10
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
            "New impression text, complete and self-contained description (max 500 characters)"
        ),
                ),
            },
            required=["impression"],
        ),
    ),
    types.FunctionDeclaration(
        name="fetch_permanent_summaries",
        description=(
            "Fetch user's historical conversation summaries (newest on top, max 10 results per request)"),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "start": types.Schema(
                    type=types.Type.INTEGER,
                    description=("Start position (inclusive)"),
                    default=1
                ),
                "end": types.Schema(
                    type=types.Type.INTEGER,
                    description=("End position (inclusive)"),
                    default=2
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
