import json
from typing import Any, Iterable, Optional

from sqlalchemy.engine import Result

from . import config, db
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

    def _trim_messages_with_tool_context(
        messages: list[dict],
        keep_non_tool: int = 10,
    ) -> list[dict]:
        if not messages:
            return []

        non_tool_indices: list[int] = []
        for idx, msg in enumerate(messages):
            if not isinstance(msg, dict):
                non_tool_indices.append(idx)
                continue
            if msg.get("role") != "tool":
                non_tool_indices.append(idx)
        if len(non_tool_indices) <= keep_non_tool:
            return list(messages)

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
            return trimmed

        indices = sorted(set(range(start_idx, len(messages))) | required_indices)
        return [messages[i] for i in indices]

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
        if overflow:
            snapshot_value = json.dumps(messages_with_new, ensure_ascii=False)
            if snapshot_value:
                await connection.exec_driver_sql(
                    "INSERT INTO permanent_chat_records (user_id, conversation_snapshot) VALUES (%s, %s)",
                    (conversation_id, snapshot_value),
                )
                archived_records = await prune_permanent_records(
                    conversation_id,
                    connection=connection,
                )
                snapshot_created = True
            warning_level = "overflow"
        elif token_count > config.CHAT_TOKEN_WARN_LIMIT:
            warning_level = "near_limit"

        if overflow:
            messages = _trim_messages_with_tool_context(messages_with_new)
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


async def check_user_exists(user_id: int) -> bool:
    row = await fetch_one("SELECT id FROM user WHERE id = %s", (user_id,))
    return row is not None


async def async_check_user_exists(user_id: int) -> bool:
    return await check_user_exists(user_id)
