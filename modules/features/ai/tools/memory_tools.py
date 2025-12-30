import json
import re
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
        return {"user_id": None, "error": "Missing user information, cannot retrieve summaries"}

    try:
        start_idx = int(start) if start is not None else 1
    except (TypeError, ValueError):
        start_idx = 1

    try:
        end_idx = int(end) if end is not None else start_idx
    except (TypeError, ValueError):
        end_idx = start_idx

    if start_idx < 1:
        start_idx = 1
    if end_idx < start_idx:
        end_idx = start_idx

    window_size = end_idx - start_idx + 1
    window_size = max(1, min(window_size, 5))
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


def search_permanent_records_tool(
    pattern: str,
    limit: Optional[int] = None,
    oldest_first: Optional[bool] = None,
    **kwargs,
) -> dict:
    """Search user's permanent conversation snapshots with a regex pattern."""
    context = get_tool_request_context()
    user_id = context.get("user_id")
    if not user_id:
        return {"user_id": None, "error": "Missing user information, cannot search records"}

    if not isinstance(pattern, str) or not pattern.strip():
        return {"user_id": user_id, "error": "Missing search pattern"}

    try:
        limit_value = int(limit) if limit is not None else 5
    except (TypeError, ValueError):
        limit_value = 5
    limit_value = max(1, min(limit_value, 50))

    oldest_first_value = False
    if isinstance(oldest_first, bool):
        oldest_first_value = oldest_first
    elif isinstance(oldest_first, str):
        oldest_first_value = oldest_first.strip().lower() in {"1", "true", "yes", "y"}

    warning = None
    try:
        matcher = re.compile(pattern, re.IGNORECASE | re.DOTALL)
    except re.error:
        warning = "Invalid regex pattern, treated as literal string"
        matcher = re.compile(re.escape(pattern), re.IGNORECASE | re.DOTALL)

    batch_size = 50

    def _fetch_rows(offset: int) -> list[tuple]:
        return mysql_connection.run_sync(
            mysql_connection.fetch_all(
                """
                SELECT id, conversation_snapshot, created_at
                FROM permanent_chat_records
                WHERE user_id = %s
                ORDER BY created_at DESC, id DESC
                LIMIT %s OFFSET %s
                """,
                (user_id, batch_size, offset),
            )
        )

    def _scan_rows(rows: list[tuple], results: list[dict], offset: int) -> list[dict]:
        for row_index, row in enumerate(rows):
            _record_id, snapshot_text, created_at = row
            if isinstance(snapshot_text, bytes):
                snapshot_text = snapshot_text.decode("utf-8")

            try:
                messages = json.loads(snapshot_text) if isinstance(snapshot_text, str) else snapshot_text
            except (TypeError, ValueError, json.JSONDecodeError):
                continue

            if not isinstance(messages, list):
                continue

            filtered_messages = []
            for message in messages:
                if not isinstance(message, dict):
                    continue
                role = message.get("role")
                if role not in ("user", "assistant"):
                    continue
                content = message.get("content")
                if content is None:
                    continue
                if not isinstance(content, str):
                    content = json.dumps(content, ensure_ascii=False)
                filtered_messages.append(
                    {
                        "role": role,
                        "content": content,
                    }
                )

            if not filtered_messages:
                continue

            for idx in range(len(filtered_messages) - 1, -1, -1):
                content = filtered_messages[idx]["content"]
                if not matcher.search(content):
                    continue

                before_start = max(0, idx - 5)
                after_end = min(len(filtered_messages), idx + 6)
                before = [
                    {"index": before_start + offset, **msg}
                    for offset, msg in enumerate(filtered_messages[before_start:idx])
                ]
                after = [
                    {"index": idx + 1 + offset, **msg}
                    for offset, msg in enumerate(filtered_messages[idx + 1 : after_end])
                ]
                results.append(
                    {
                        "record_position": offset + row_index + 1,
                        "created_at": created_at.isoformat(sep=" ") if created_at else None,
                        "match": {"index": idx, **filtered_messages[idx]},
                        "before": before,
                        "after": after,
                    }
                )
                if len(results) >= limit_value:
                    return results
        return results

    results: list[dict] = []
    first_batch_offset = 0
    first_batch = _fetch_rows(first_batch_offset)
    results = _scan_rows(first_batch, results, first_batch_offset)

    if len(results) < limit_value and len(first_batch) == batch_size:
        second_batch_offset = batch_size
        second_batch = _fetch_rows(second_batch_offset)
        results = _scan_rows(second_batch, results, second_batch_offset)

    response = {
        "user_id": user_id,
        "pattern": pattern,
        "limit": limit_value,
        "oldest_first": oldest_first_value,
        "results": list(reversed(results)) if oldest_first_value else results,
    }
    if warning:
        response["warning"] = warning

    return response


__all__ = [
    "get_help_text_tool",
    "fetch_group_context_tool",
    "fetch_permanent_summaries_tool",
    "search_permanent_records_tool",
]
