import json
from datetime import datetime
from typing import Any, Iterable, Optional

from sqlalchemy.engine import Result

from . import config, db
from .prompt_utils import format_metadata_attrs, xml_escape
from .token_estimator import estimate_conversation_tokens


connect = db.connect
transaction = db.transaction
run_sync = db.run_sync

PERMANENT_RECORDS_KEEP = 100


async def fetch_one(
    sql: str,
    params: Optional[Iterable[Any]] = None,
    *,
    mapping: bool = False,
    connection=None,
):
    result: Result = await db.exec_sql(sql, params, connection=connection)
    if mapping:
        return result.mappings().first()
    return result.fetchone()


async def fetch_all(
    sql: str,
    params: Optional[Iterable[Any]] = None,
    *,
    mapping: bool = False,
    connection=None,
):
    result: Result = await db.exec_sql(sql, params, connection=connection)
    if mapping:
        return result.mappings().all()
    return result.fetchall()


async def execute(
    sql: str,
    params: Optional[Iterable[Any]] = None,
    *,
    connection=None,
) -> int:
    if connection is None:
        async with transaction() as connection:
            result: Result = await connection.exec_driver_sql(sql, params)
            return result.rowcount
    result: Result = await connection.exec_driver_sql(sql, params)
    return result.rowcount


async def prune_permanent_records(
    user_id: int,
    *,
    connection,
    keep: int = PERMANENT_RECORDS_KEEP,
) -> list[dict]:
    keep = max(1, int(keep))

    rows = await fetch_all(
        """
        SELECT id, created_at, summary, conversation_snapshot
        FROM permanent_chat_records
        WHERE user_id = %s
        ORDER BY created_at DESC, id DESC
        LIMIT %s OFFSET %s
        """,
        (user_id, keep, keep),
        connection=connection,
    )
    if not rows:
        return []

    record_ids: list[int] = []
    records: list[dict] = []
    for row in rows:
        record_id, created_at, summary_text, snapshot_text = row
        record_ids.append(record_id)

        if isinstance(summary_text, bytes):
            summary_text = summary_text.decode("utf-8")

        snapshot_value = snapshot_text
        if isinstance(snapshot_value, bytes):
            snapshot_value = snapshot_value.decode("utf-8")
        if isinstance(snapshot_value, str):
            try:
                snapshot_value = json.loads(snapshot_value)
            except (TypeError, ValueError, json.JSONDecodeError):
                pass

        records.append(
            {
                "record_id": record_id,
                "created_at": created_at.isoformat(sep=" ") if created_at else None,
                "summary": summary_text,
                "conversation_snapshot": snapshot_value,
            }
        )

    placeholders = ", ".join(["%s"] * len(record_ids))
    await connection.exec_driver_sql(
        f"DELETE FROM permanent_chat_records WHERE user_id = %s AND id IN ({placeholders})",
        (user_id, *record_ids),
    )

    records.reverse()
    return records


async def insert_chat_record(
    conversation_id,
    role,
    content,
    *,
    system_prompt_extra: str | None = None,
):
    snapshot_created = False
    warning_level = None
    archived_records: list[dict] = []

    def _is_history_state_event(message: object) -> bool:
        if not isinstance(message, dict):
            return False
        if message.get("role") != "system":
            return False
        content = message.get("content")
        if not isinstance(content, str):
            return False
        return 'origin="history_state"' in content

    def _trim_messages_with_tool_context(
        messages: list[dict],
        keep_non_tool: int = 10,
    ) -> tuple[list[dict], list[int]]:
        if not messages:
            return [], []

        non_tool_indices: list[int] = []
        for idx, msg in enumerate(messages):
            if _is_history_state_event(msg):
                continue
            if not isinstance(msg, dict):
                non_tool_indices.append(idx)
                continue
            if msg.get("role") != "tool":
                non_tool_indices.append(idx)
        if len(non_tool_indices) <= keep_non_tool:
            indices = list(range(len(messages)))
            return list(messages), indices

        start_idx = non_tool_indices[-keep_non_tool]
        trimmed = messages[start_idx:]

        tool_calls_in_trimmed: set[str] = set()
        for msg in trimmed:
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            for call in msg.get("tool_calls") or []:
                call_id = call.get("id")
                if call_id:
                    tool_calls_in_trimmed.add(call_id)

        tool_call_index: dict[str, int] = {}
        for idx, msg in enumerate(messages):
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            for call in msg.get("tool_calls") or []:
                call_id = call.get("id")
                if call_id and call_id not in tool_call_index:
                    tool_call_index[call_id] = idx

        required_indices: set[int] = set()
        for msg in trimmed:
            if not isinstance(msg, dict) or msg.get("role") != "tool":
                continue
            call_id = msg.get("tool_call_id")
            if call_id and call_id not in tool_calls_in_trimmed:
                call_idx = tool_call_index.get(call_id)
                if call_idx is not None:
                    required_indices.add(call_idx)

        if not required_indices:
            indices = list(range(start_idx, len(messages)))
            return trimmed, indices

        indices = sorted(set(range(start_idx, len(messages))) | required_indices)
        return [messages[i] for i in indices], indices

    def _build_history_state_event(
        state: str,
        *,
        summary_text: str | None = None,
    ) -> dict:
        attrs = [
            ("type", "system"),
            ("timestamp", datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")),
            ("origin", "history_state"),
            ("history_state", state),
        ]
        attr_text = format_metadata_attrs(attrs)
        lines = [f"<metadata {attr_text}>"]
        if summary_text:
            lines.append(f"  <summary>{xml_escape(summary_text)}</summary>")
        lines.append("</metadata>")
        return {
            "role": "system",
            "content": "\n".join(lines),
        }

    def _find_last_user_message_index(messages: list[dict]) -> int | None:
        for idx in range(len(messages) - 1, -1, -1):
            msg = messages[idx]
            if not isinstance(msg, dict):
                continue
            if msg.get("role") != "user":
                continue
            if isinstance(msg.get("content"), str):
                return idx
        return None

    def _last_history_state_event(messages: list[dict]) -> str | None:
        for msg in reversed(messages):
            if not _is_history_state_event(msg):
                continue
            content = msg.get("content") or ""
            marker = 'history_state="'
            start_idx = content.find(marker)
            if start_idx == -1:
                return None
            value_start = start_idx + len(marker)
            value_end = content.find('"', value_start)
            if value_end == -1:
                return None
            return content[value_start:value_end]
        return None

    async with transaction() as connection:
        row = await fetch_one(
            "SELECT messages FROM chat_records WHERE conversation_id = %s",
            (conversation_id,),
            connection=connection,
        )

        raw_messages = None
        if row:
            raw_messages = row[0]
            if isinstance(raw_messages, bytes):
                raw_messages = raw_messages.decode("utf-8")
            messages = json.loads(raw_messages)
        else:
            messages = []

        if not isinstance(messages, list):
            messages = []

        is_new_session = not messages

        if not isinstance(content, dict) or not content.get("role"):
            message_entry = {"role": role, "content": content}
        else:
            message_entry = content

        messages_with_new = list(messages)
        messages_with_new.append(message_entry)

        token_count = estimate_conversation_tokens(
            messages_with_new,
            system_prompt=config.SYSTEM_PROMPT,
            system_prompt_extra=system_prompt_extra,
        )
        overflow = token_count > config.CHAT_TOKEN_LIMIT
        trimmed_messages: list[dict] | None = None
        kept_indices: list[int] | None = None
        if overflow:
            warning_level = "overflow"
        elif token_count > config.CHAT_TOKEN_WARN_LIMIT:
            warning_level = "near_limit"

        event_state = None
        compressed_event: dict | None = None
        if warning_level == "overflow":
            event_state = "compressed"
        elif role == "user":
            if warning_level == "near_limit":
                event_state = "near_limit"
            elif is_new_session:
                event_state = "new_session"

        if event_state in {"near_limit", "new_session"}:
            last_event_state = _last_history_state_event(messages_with_new)
            if last_event_state == event_state:
                event_state = None

        if event_state == "compressed":
            compressed_event = _build_history_state_event(event_state)
            event_state = None

        if event_state:
            target_index = _find_last_user_message_index(messages_with_new)
            if target_index is not None:
                event_message = _build_history_state_event(event_state)
                messages_with_new.insert(target_index + 1, event_message)

        if overflow:
            trimmed_messages, kept_indices = _trim_messages_with_tool_context(messages_with_new)
            trimmed_messages = [
                msg for msg in trimmed_messages if not _is_history_state_event(msg)
            ]
            if compressed_event:
                trimmed_messages.insert(0, compressed_event)
            kept_set = set(kept_indices)
            archived_messages = [
                messages_with_new[idx]
                for idx in range(len(messages_with_new))
                if idx not in kept_set
            ]
            snapshot_value = json.dumps(archived_messages, ensure_ascii=False)
            await connection.exec_driver_sql(
                "INSERT INTO permanent_chat_records (user_id, conversation_snapshot) VALUES (%s, %s)",
                (conversation_id, snapshot_value),
            )
            archived_records = await prune_permanent_records(
                conversation_id,
                connection=connection,
            )
            snapshot_created = True

        if overflow:
            if trimmed_messages is None:
                trimmed_messages, _ = _trim_messages_with_tool_context(messages_with_new)
            messages = trimmed_messages
        else:
            messages = messages_with_new

        if row:
            if overflow:
                await connection.exec_driver_sql(
                    "UPDATE chat_records SET messages = %s, last_rotated_at = CURRENT_TIMESTAMP "
                    "WHERE conversation_id = %s",
                    (json.dumps(messages, ensure_ascii=False), conversation_id),
                )
            else:
                await connection.exec_driver_sql(
                    "UPDATE chat_records SET messages = %s WHERE conversation_id = %s",
                    (json.dumps(messages, ensure_ascii=False), conversation_id),
                )
        else:
            if overflow:
                await connection.exec_driver_sql(
                    "INSERT INTO chat_records (conversation_id, messages, last_rotated_at) "
                    "VALUES (%s, %s, CURRENT_TIMESTAMP)",
                    (conversation_id, json.dumps(messages, ensure_ascii=False)),
                )
            else:
                await connection.exec_driver_sql(
                    "INSERT INTO chat_records (conversation_id, messages) VALUES (%s, %s)",
                    (conversation_id, json.dumps(messages, ensure_ascii=False)),
                )

    return snapshot_created, warning_level, archived_records


async def async_insert_chat_record(
    conversation_id,
    role,
    content,
    *,
    system_prompt_extra: str | None = None,
):
    return await insert_chat_record(
        conversation_id,
        role,
        content,
        system_prompt_extra=system_prompt_extra,
    )


async def get_chat_history(conversation_id):
    row = await fetch_one(
        "SELECT messages FROM chat_records WHERE conversation_id = %s",
        (conversation_id,),
    )
    if row:
        return json.loads(row[0])
    return []


async def async_get_chat_history(conversation_id):
    return await get_chat_history(conversation_id)


async def async_update_latest_history_state_summary(
    conversation_id: int,
    summary_text: str,
) -> bool:
    if not summary_text:
        return False

    row = await fetch_one(
        "SELECT messages FROM chat_records WHERE conversation_id = %s",
        (conversation_id,),
    )
    if not row or not row[0]:
        return False

    raw_messages = row[0]
    if isinstance(raw_messages, bytes):
        raw_messages = raw_messages.decode("utf-8")
    try:
        messages = json.loads(raw_messages)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False

    if not isinstance(messages, list):
        return False

    updated = False
    for idx in range(len(messages) - 1, -1, -1):
        msg = messages[idx]
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "system":
            continue
        content = msg.get("content")
        if not isinstance(content, str):
            continue
        if 'origin="history_state"' not in content:
            continue
        if 'history_state="compressed"' not in content:
            continue
        if "<summary>" in content:
            break
        end_tag = "</metadata>"
        insert_idx = content.find(end_tag)
        if insert_idx == -1:
            break
        summary_line = f"  <summary>{xml_escape(summary_text)}</summary>\n"
        msg["content"] = f"{content[:insert_idx]}{summary_line}{content[insert_idx:]}"
        messages[idx] = msg
        updated = True
        break

    if not updated:
        return False

    await execute(
        "UPDATE chat_records SET messages = %s WHERE conversation_id = %s",
        (json.dumps(messages, ensure_ascii=False), conversation_id),
    )
    return True


async def check_user_exists(user_id: int) -> bool:
    row = await fetch_one("SELECT id FROM user WHERE id = %s", (user_id,))
    return row is not None


async def async_check_user_exists(user_id: int) -> bool:
    return await check_user_exists(user_id)
