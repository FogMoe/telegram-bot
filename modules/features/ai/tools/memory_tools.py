from typing import Dict, Optional

from core import config, group_chat_history, mysql_connection

from .context import get_tool_request_context


def get_help_text_tool() -> dict:
    """Return the configured help command list for the bot."""
    return {"help_text": config.HELP_TEXT}


def fetch_group_context_tool(
    window_size: int = 10,
    **kwargs,
) -> dict:
    """Retrieve recent messages before the current group chat message."""
    context = get_tool_request_context()
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


def fetch_permanent_summaries_tool(
    start: Optional[int] = None,
    end: Optional[int] = None,
    **kwargs,
) -> dict:
    """Retrieve current user's permanent conversation summaries."""
    context = get_tool_request_context()
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

    total_row = mysql_connection.run_sync(
        mysql_connection.fetch_one(
            "SELECT COUNT(*) FROM permanent_chat_records WHERE user_id = %s AND summary IS NOT NULL AND summary != ''",
            (user_id,),
        )
    )
    total_rows = total_row[0] if total_row and total_row[0] is not None else 0

    rows = mysql_connection.run_sync(
        mysql_connection.fetch_all(
            """
            SELECT id, summary, created_at
            FROM permanent_chat_records
            WHERE user_id = %s AND summary IS NOT NULL AND summary != ''
            ORDER BY created_at DESC, id DESC
            LIMIT %s OFFSET %s
            """,
            (user_id, window_size, offset),
        )
    )

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


__all__ = [
    "get_help_text_tool",
    "fetch_group_context_tool",
    "fetch_permanent_summaries_tool",
]
