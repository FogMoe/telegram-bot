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
        if len(current_payload) > 120000:
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
            messages = []
        elif len(current_payload) > 110000:
            warning_level = "near_limit"

        if isinstance(content, dict) and content.get("role"):
            message_entry = content
        else:
            message_entry = {"role": role, "content": content}

        messages.append(message_entry)

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
