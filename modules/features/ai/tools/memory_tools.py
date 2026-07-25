import json
import re
from typing import Dict, Optional

from core import config, group_chat_history, mysql_connection

from .context import get_tool_request_context

MAX_USER_DIARY_PAGE_CHARS = 10000
MAX_USER_DIARY_PAGES = 100
MAX_USER_DIARY_TITLE_CHARS = 60
MAX_USER_DIARY_SUMMARY_CHARS = 120
USER_DIARY_INDEX_PREVIEW_CHARS = 500


def _diary_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _diary_timestamp(value: object) -> str | None:
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return isoformat(sep=" ")
    return str(value)


def _diary_page_metadata(
    page_no: int,
    title: object,
    summary: object,
    preview_content: object,
) -> dict:
    stored_title = _diary_text(title).strip()
    stored_summary = _diary_text(summary).strip()
    preview = re.sub(r"\s+", " ", _diary_text(preview_content)).strip()
    return {
        "title": stored_title or f"Untitled page {page_no}",
        "summary": stored_summary or preview[:MAX_USER_DIARY_SUMMARY_CHARS],
        "metadata_complete": bool(stored_title and stored_summary),
    }


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

    total_row = mysql_connection.run_sync(
        mysql_connection.fetch_one(
            "SELECT COUNT(*) FROM permanent_chat_records WHERE user_id = %s",
            (user_id,),
        )
    )
    total_rows = total_row[0] if total_row and total_row[0] is not None else 0
    if total_rows <= 0:
        response = {
            "user_id": user_id,
            "pattern": pattern,
            "limit": limit_value,
            "oldest_first": oldest_first_value,
            "results": [],
        }
        if warning:
            response["warning"] = warning
        return response

    max_records = mysql_connection.PERMANENT_RECORDS_KEEP
    try:
        limit_row = mysql_connection.run_sync(
            mysql_connection.fetch_one(
                "SELECT permanent_records_limit FROM user WHERE id = %s",
                (user_id,),
            )
        )
    except Exception:
        limit_row = None
    if limit_row and limit_row[0] is not None:
        try:
            max_records = int(limit_row[0])
        except (TypeError, ValueError):
            max_records = mysql_connection.PERMANENT_RECORDS_KEEP
    max_records = max(1, max_records)

    scan_limit = min(max_records, total_rows)

    order_clause = "ORDER BY created_at ASC, id ASC"
    if not oldest_first_value:
        order_clause = "ORDER BY created_at DESC, id DESC"

    batch_size = 50

    def _fetch_rows(offset: int, size: int) -> list[tuple]:
        return mysql_connection.run_sync(
            mysql_connection.fetch_all(
                f"""
                SELECT id, conversation_snapshot, created_at
                FROM permanent_chat_records
                WHERE user_id = %s
                {order_clause}
                LIMIT %s OFFSET %s
                """,
                (user_id, size, offset),
            )
        )

    def _record_position(offset: int, row_index: int) -> int:
        if oldest_first_value:
            return total_rows - (offset + row_index)
        return offset + row_index + 1

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
                if role == "user" and 'origin="history_state"' in content:
                    continue
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
                        "record_position": _record_position(offset, row_index),
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
    offset = 0
    remaining = scan_limit
    while remaining > 0 and len(results) < limit_value:
        fetch_size = min(batch_size, remaining)
        rows = _fetch_rows(offset, fetch_size)
        if not rows:
            break
        results = _scan_rows(rows, results, offset)
        if len(rows) < fetch_size:
            break
        offset += fetch_size
        remaining -= fetch_size

    response = {
        "user_id": user_id,
        "pattern": pattern,
        "limit": limit_value,
        "oldest_first": oldest_first_value,
        "results": results,
    }
    if warning:
        response["warning"] = warning

    return response


def user_diary_tool(
    action: Optional[str] = None,
    content: Optional[str] = None,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
    line_numbers: Optional[bool] = None,
    page: Optional[int] = None,
    title: Optional[str] = None,
    summary: Optional[str] = None,
    **kwargs,
) -> dict:
    """Read or update the internal diary for the current user."""
    context = get_tool_request_context()
    user_id = context.get("user_id")
    if not user_id:
        return {"user_id": None, "error": "Missing user information, cannot access diary"}

    action_value = (action or "read").strip().lower()
    if action_value in {"index", "list", "catalog", "toc"}:
        action_value = "index"
    elif action_value in {"read", "view", "get"}:
        action_value = "read"
    elif action_value in {"append", "add", "increment"}:
        action_value = "append"
    elif action_value in {"patch", "edit", "update", "modify"}:
        action_value = "patch"
    elif action_value in {"overwrite", "replace", "set"}:
        action_value = "overwrite"
    else:
        return {"user_id": user_id, "error": f"Unknown action: {action}"}

    if action_value == "index":
        rows = mysql_connection.run_sync(
            mysql_connection.fetch_all(
                "SELECT page_no, title, summary, CHAR_LENGTH(content), "
                "created_at, updated_at, LEFT(content, %s) "
                "FROM ai_user_diary_pages WHERE user_id = %s ORDER BY page_no ASC",
                (USER_DIARY_INDEX_PREVIEW_CHARS, user_id),
            )
        )
        pages = []
        max_page = 0
        for row in rows:
            (
                page_no,
                stored_title,
                stored_summary,
                content_length,
                created_at,
                updated_at,
                preview_content,
            ) = row
            page_no = int(page_no)
            max_page = max(max_page, page_no)
            pages.append(
                {
                    "page": page_no,
                    **_diary_page_metadata(
                        page_no,
                        stored_title,
                        stored_summary,
                        preview_content,
                    ),
                    "length": int(content_length or 0),
                    "created_at": _diary_timestamp(created_at),
                    "updated_at": _diary_timestamp(updated_at),
                }
            )

        response = {
            "user_id": user_id,
            "action": "index",
            "total_pages": max_page,
            "next_page": max_page + 1 if max_page < MAX_USER_DIARY_PAGES else None,
            "pages": pages,
        }
        ignored_fields = []
        if page is not None:
            ignored_fields.append("page")
        if content is not None:
            ignored_fields.append("content")
        if start_line is not None or end_line is not None:
            ignored_fields.append("line range")
        if line_numbers is not None:
            ignored_fields.append("line_numbers")
        if title is not None:
            ignored_fields.append("title")
        if summary is not None:
            ignored_fields.append("summary")
        if ignored_fields:
            response["warning"] = f"{', '.join(ignored_fields)} ignored for index action"
        return response

    try:
        page_value = int(page) if page is not None else 1
    except (TypeError, ValueError):
        return {"user_id": user_id, "error": "Invalid page number"}
    if page_value < 1 or page_value > MAX_USER_DIARY_PAGES:
        return {"user_id": user_id, "error": f"Page number out of range (max={MAX_USER_DIARY_PAGES})"}

    max_page_row = mysql_connection.run_sync(
        mysql_connection.fetch_one(
            "SELECT MAX(page_no) FROM ai_user_diary_pages WHERE user_id = %s",
            (user_id,),
        )
    )
    max_page = max_page_row[0] if max_page_row and max_page_row[0] is not None else 0

    row = mysql_connection.run_sync(
        mysql_connection.fetch_one(
            "SELECT content, title, summary, created_at, updated_at FROM ai_user_diary_pages "
            "WHERE user_id = %s AND page_no = %s",
            (user_id, page_value),
        )
    )

    diary_content = ""
    stored_title = ""
    stored_summary = ""
    created_at = None
    updated_at = None
    page_exists = False
    if row:
        page_exists = True
        diary_content, stored_title, stored_summary, created_at, updated_at = row
        diary_content = _diary_text(diary_content)
        stored_title = _diary_text(stored_title).strip()
        stored_summary = _diary_text(stored_summary).strip()

    warnings: list[str] = []
    if action_value == "read" and content is not None:
        warnings.append("content ignored for read action")
    if action_value == "read" and title is not None:
        warnings.append("title ignored for read action")
    if action_value == "read" and summary is not None:
        warnings.append("summary ignored for read action")
    if action_value in {"append", "overwrite"} and (start_line is not None or end_line is not None):
        warnings.append("line range ignored for append/overwrite action")

    if action_value == "read":
        line_numbers_value = False
        if isinstance(line_numbers, bool):
            line_numbers_value = line_numbers
        elif isinstance(line_numbers, str):
            line_numbers_value = line_numbers.strip().lower() in {"1", "true", "yes", "y"}

        lines = diary_content.splitlines()
        total_lines = len(lines)
        content_length = len(diary_content)
        metadata = _diary_page_metadata(
            page_value,
            stored_title,
            stored_summary,
            diary_content,
        )

        if start_line is None and end_line is None:
            response = {
                "user_id": user_id,
                "action": "read",
                "page": page_value,
                "total_pages": max_page,
                **metadata,
                "total_lines": total_lines,
                "length": content_length,
                "content": diary_content,
                "created_at": _diary_timestamp(created_at),
                "updated_at": _diary_timestamp(updated_at),
            }
            if line_numbers_value:
                response["lines"] = [
                    {"line": idx + 1, "content": line}
                    for idx, line in enumerate(lines)
                ]
            if warnings:
                response["warning"] = "; ".join(warnings)
            return response

        try:
            start_value = int(start_line) if start_line is not None else 1
            end_value = int(end_line) if end_line is not None else total_lines
        except (TypeError, ValueError):
            return {"user_id": user_id, "error": "Invalid line range"}

        if total_lines == 0:
            response = {
                "user_id": user_id,
                "action": "read",
                "page": page_value,
                "total_pages": max_page,
                **metadata,
                "total_lines": 0,
                "length": 0,
                "range": {"start_line": 0, "end_line": 0},
                "content": "",
                "created_at": _diary_timestamp(created_at),
                "updated_at": _diary_timestamp(updated_at),
            }
            if line_numbers_value:
                response["lines"] = []
            if warnings:
                response["warning"] = "; ".join(warnings)
            return response

        if start_value < 1:
            start_value = 1
        if end_value < start_value:
            return {"user_id": user_id, "error": "Invalid line range"}
        if total_lines and end_value > total_lines:
            end_value = total_lines

        selected_lines = lines[start_value - 1 : end_value] if total_lines else []
        response = {
            "user_id": user_id,
            "action": "read",
            "page": page_value,
            "total_pages": max_page,
            **metadata,
            "total_lines": total_lines,
            "length": content_length,
            "range": {"start_line": start_value, "end_line": end_value},
            "content": "\n".join(selected_lines),
            "created_at": _diary_timestamp(created_at),
            "updated_at": _diary_timestamp(updated_at),
        }
        if line_numbers_value:
            response["lines"] = [
                {"line": start_value + idx, "content": line}
                for idx, line in enumerate(selected_lines)
            ]
        if warnings:
            response["warning"] = "; ".join(warnings)
        return response

    if not page_exists:
        if max_page >= MAX_USER_DIARY_PAGES:
            return {"user_id": user_id, "error": f"Diary page limit reached (max={MAX_USER_DIARY_PAGES})"}
        if page_value > max_page + 1:
            return {
                "user_id": user_id,
                "error": f"Page out of range; create next page first (max={MAX_USER_DIARY_PAGES})",
            }

    if content is None:
        return {"user_id": user_id, "error": "Missing content for diary update"}

    title_value = _diary_text(title).strip()
    summary_value = _diary_text(summary).strip()
    if len(title_value) > MAX_USER_DIARY_TITLE_CHARS:
        return {
            "user_id": user_id,
            "error": f"Diary page title is too long (max={MAX_USER_DIARY_TITLE_CHARS})",
        }
    if len(summary_value) > MAX_USER_DIARY_SUMMARY_CHARS:
        return {
            "user_id": user_id,
            "error": f"Diary page summary is too long (max={MAX_USER_DIARY_SUMMARY_CHARS})",
        }
    if not summary_value:
        return {
            "user_id": user_id,
            "error": f"Missing summary for diary page {page_value}; summarize the updated page",
        }
    if not title_value and not stored_title:
        return {
            "user_id": user_id,
            "error": f"Missing title for diary page {page_value}; add a stable topic title",
        }
    effective_title = title_value or stored_title

    content_value = content if isinstance(content, str) else str(content)
    if action_value == "patch":
        lines = diary_content.splitlines()
        total_lines = len(lines)
        if start_line is None or end_line is None:
            return {"user_id": user_id, "error": "Missing line range for patch"}
        try:
            start_value = int(start_line)
            end_value = int(end_line)
        except (TypeError, ValueError):
            return {"user_id": user_id, "error": "Invalid line range"}

        if start_value < 1 or end_value < start_value:
            return {"user_id": user_id, "error": "Invalid line range"}
        if start_value > total_lines + 1:
            return {"user_id": user_id, "error": "Line range out of bounds"}

        start_idx = start_value - 1
        end_idx = min(end_value, total_lines)
        replacement_lines = content_value.splitlines()
        lines[start_idx:end_idx] = replacement_lines
        merged_content = "\n".join(lines)
    elif action_value == "append":
        if diary_content and not diary_content.endswith("\n"):
            merged_content = f"{diary_content}\n{content_value}"
        else:
            merged_content = f"{diary_content}{content_value}"
    else:
        merged_content = content_value

    truncated = False
    if len(merged_content) > MAX_USER_DIARY_PAGE_CHARS:
        merged_content = merged_content[-MAX_USER_DIARY_PAGE_CHARS:]
        truncated = True

    mysql_connection.run_sync(
        mysql_connection.execute(
            """
            INSERT INTO ai_user_diary_pages (user_id, page_no, title, summary, content)
            VALUES (%s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                title = VALUES(title),
                summary = VALUES(summary),
                content = VALUES(content),
                updated_at = CURRENT_TIMESTAMP
            """,
            (user_id, page_value, effective_title, summary_value, merged_content),
        )
    )

    total_lines = len(merged_content.splitlines())
    updated_total_pages = max(max_page, page_value)
    response = {
        "user_id": user_id,
        "action": action_value,
        "page": page_value,
        "total_pages": updated_total_pages,
        "title": effective_title,
        "summary": summary_value,
        "metadata_complete": True,
        "total_lines": total_lines,
        "length": len(merged_content),
        "truncated": truncated,
    }
    if truncated:
        warnings.append(
            f"Diary exceeded {MAX_USER_DIARY_PAGE_CHARS} chars, truncated oldest content"
        )
    if warnings:
        response["warning"] = "; ".join(warnings)
    return response


__all__ = [
    "get_help_text_tool",
    "fetch_group_context_tool",
    "fetch_permanent_summaries_tool",
    "search_permanent_records_tool",
    "user_diary_tool",
]
