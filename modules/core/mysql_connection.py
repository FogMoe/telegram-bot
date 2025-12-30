import json
from typing import Any, Iterable, Optional

from sqlalchemy.engine import Result

from . import db


connect = db.connect
transaction = db.transaction
run_sync = db.run_sync


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


async def insert_chat_record(conversation_id, role, content):
    snapshot_created = False
    warning_level = None

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

        current_payload = json.dumps(messages, ensure_ascii=False)
        overflow = len(current_payload) > 120000
        if overflow:
            if row and raw_messages:
                snapshot_value = (
                    raw_messages
                    if isinstance(raw_messages, str)
                    else json.dumps(messages, ensure_ascii=False)
                )
                await connection.exec_driver_sql(
                    "INSERT INTO permanent_chat_records (user_id, conversation_snapshot) VALUES (%s, %s)",
                    (conversation_id, snapshot_value),
                )
                await connection.exec_driver_sql(
                    """
                    DELETE FROM permanent_chat_records
                    WHERE user_id = %s
                    AND id NOT IN (
                        SELECT recent.id FROM (
                            SELECT id FROM permanent_chat_records
                            WHERE user_id = %s
                            ORDER BY created_at DESC, id DESC
                            LIMIT 100
                        ) AS recent
                    )
                    """,
                    (conversation_id, conversation_id),
                )
                snapshot_created = True
            warning_level = "overflow"
        elif len(current_payload) > 110000:
            warning_level = "near_limit"

        if isinstance(content, dict) and content.get("role"):
            message_entry = content
        else:
            message_entry = {"role": role, "content": content}

        messages.append(message_entry)
        if overflow:
            messages = _trim_messages_with_tool_context(messages)

        if row:
            await connection.exec_driver_sql(
                "UPDATE chat_records SET messages = %s WHERE conversation_id = %s",
                (json.dumps(messages, ensure_ascii=False), conversation_id),
            )
        else:
            await connection.exec_driver_sql(
                "INSERT INTO chat_records (conversation_id, messages) VALUES (%s, %s)",
                (conversation_id, json.dumps(messages, ensure_ascii=False)),
            )

    return snapshot_created, warning_level


async def async_insert_chat_record(conversation_id, role, content):
    return await insert_chat_record(conversation_id, role, content)


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
