import mysql.connector
from mysql.connector import Error
import json
import asyncio
from concurrent.futures import ThreadPoolExecutor
from . import config

# 创建线程池执行器用于异步数据库操作
db_executor = ThreadPoolExecutor(max_workers=5)

def create_connection(
        host_name=config.MYSQL_CONFIG['host'],
        user_name=config.MYSQL_CONFIG['user'],
        user_password=config.MYSQL_CONFIG['password'],
        db_name=config.MYSQL_CONFIG['database']
):
    connection = None
    try:
        connection = mysql.connector.connect(
            host=host_name,
            user=user_name,
            passwd=user_password,
            database=db_name
        )
        # print("Connection to MySQL DB successful")
    except Error as e:
        print(f"The error '{e}' occurred")

    return connection


# Function to insert chat record into the database
def insert_chat_record(conversation_id, role, content):
    connection = create_connection()
    cursor = connection.cursor()

    snapshot_created = False
    warning_level = None

    # 使用参数化查询，防止SQL注入
    select_query = "SELECT messages FROM chat_records WHERE conversation_id = %s"
    cursor.execute(select_query, (conversation_id,))
    result = cursor.fetchone()

    raw_messages = None
    if result:
        raw_messages = result[0]
        if isinstance(raw_messages, bytes):
            raw_messages = raw_messages.decode('utf-8')
        messages = json.loads(raw_messages)
    else:
        messages = []

    # Check if messages length exceeds thresholds
    current_payload = json.dumps(messages, ensure_ascii=False)
    if len(current_payload) > 120000:
        if result and raw_messages:
            snapshot_value = raw_messages if isinstance(raw_messages, str) else json.dumps(messages, ensure_ascii=False)
            cursor.execute(
                "INSERT INTO permanent_chat_records (user_id, conversation_snapshot) VALUES (%s, %s)",
                (conversation_id, snapshot_value),
            )
            cursor.execute(
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

    # Append new message
    if isinstance(content, dict) and content.get("role"):
        message_entry = content
    else:
        message_entry = {"role": role, "content": content}

    messages.append(message_entry)

    # Insert or update the record
    if result:
        update_query = "UPDATE chat_records SET messages = %s WHERE conversation_id = %s"
        cursor.execute(update_query, (json.dumps(messages, ensure_ascii=False), conversation_id))
    else:
        insert_query = "INSERT INTO chat_records (conversation_id, messages) VALUES (%s, %s)"
        cursor.execute(insert_query, (conversation_id, json.dumps(messages, ensure_ascii=False)))

    connection.commit()
    cursor.close()
    connection.close()

    return snapshot_created, warning_level


async def async_insert_chat_record(conversation_id, role, content):
    """异步插入聊天记录"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        db_executor,
        lambda: insert_chat_record(conversation_id, role, content)
    )


# Function to get chat history from the database
def get_chat_history(conversation_id):
    connection = create_connection()
    cursor = connection.cursor()

    select_query = "SELECT messages FROM chat_records WHERE conversation_id = %s"
    cursor.execute(select_query, (conversation_id,))
    result = cursor.fetchone()

    cursor.close()
    connection.close()

    if result:
        return json.loads(result[0])
    else:
        return []


async def async_get_chat_history(conversation_id):
    """异步获取聊天历史"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        db_executor,
        lambda: get_chat_history(conversation_id)
    )


def check_user_exists(user_id: int) -> bool:
    """检查用户是否已注册"""
    connection = create_connection()
    cursor = connection.cursor()
    try:
        # 安全的参数化查询
        cursor.execute("SELECT id FROM user WHERE id = %s", (user_id,))
        return cursor.fetchone() is not None
    finally:
        cursor.close()
        connection.close()


async def async_check_user_exists(user_id: int) -> bool:
    """异步检查用户是否已注册"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        db_executor,
        lambda: check_user_exists(user_id)
    )
