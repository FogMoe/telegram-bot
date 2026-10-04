"""并发加固：对话首次写入的竞争，以及死锁错误码的识别（真实 MySQL）。"""

import asyncio
import json

from economy_support import gather_all
from mysql_support import fetch, run

from core import chat_records, db, sql


def stored_messages(url, conversation_id):
    rows = fetch(
        url, "SELECT messages FROM chat_records WHERE conversation_id = %s", (conversation_id,)
    )
    assert len(rows) == 1, rows
    messages = rows[0]["messages"]
    return json.loads(messages) if isinstance(messages, str) else messages


def contents(messages):
    """用户写入的内容；历史状态事件（`<metadata ...>`）不算。"""
    return [
        message["content"]
        for message in messages
        if message.get("role") == "user" and not message["content"].startswith("<metadata")
    ]


def test_concurrent_first_writes_to_one_conversation_all_land(app_database):
    async def scenario():
        return await gather_all(
            *[chat_records.insert_chat_record(2001, "user", f"m{index}") for index in range(8)]
        )

    results = run(scenario())

    assert all(not isinstance(item, Exception) for item in results), results
    assert sorted(contents(stored_messages(app_database, 2001))) == sorted(
        f"m{index}" for index in range(8)
    )


def test_concurrent_first_writes_across_conversations_do_not_interfere(app_database):
    async def scenario():
        writes = [
            chat_records.insert_chat_record(conversation_id, "user", f"c{conversation_id}-{index}")
            for conversation_id in (3001, 3002, 3003)
            for index in range(4)
        ]
        return await gather_all(*writes)

    results = run(scenario())

    assert all(not isinstance(item, Exception) for item in results), results
    for conversation_id in (3001, 3002, 3003):
        assert len(contents(stored_messages(app_database, conversation_id))) == 4


def test_concurrent_first_clear_of_one_conversation_leaves_a_single_row(app_database):
    async def scenario():
        return await gather_all(
            *[
                chat_records.archive_chat_and_start_new_session(4001, [("user", f"bye{index}")])
                for index in range(4)
            ]
        )

    results = run(scenario())

    assert all(not isinstance(item, Exception) for item in results), results
    assert len(stored_messages(app_database, 4001)) >= 1


def test_a_real_deadlock_is_recognised_by_its_error_code(app_database):
    async def scenario():
        a_holds_first = asyncio.Event()
        b_holds_first = asyncio.Event()

        async def worker(first: int, second: int, mine: asyncio.Event, theirs: asyncio.Event):
            async with db.transaction() as connection:
                await connection.exec_driver_sql(
                    "INSERT INTO `user` (id, name) VALUES (%s, 'x')", (first,)
                )
                mine.set()
                await theirs.wait()
                await connection.exec_driver_sql(
                    "INSERT INTO `user` (id, name) VALUES (%s, 'x')", (second,)
                )

        # 两个事务各自先插入自己的行，再插入对方的行：对方的行被未提交的插入锁住，互相等待。
        # 第二步用的是同一个主键，等待对方的事务结束后要么死锁，要么重复键。
        return await gather_all(
            worker(1, 2, a_holds_first, b_holds_first),
            worker(2, 1, b_holds_first, a_holds_first),
        )

    results = run(scenario())

    errors = [item for item in results if isinstance(item, Exception)]
    assert errors, "两个事务不可能都成功"
    assert all(sql.is_deadlock_error(error) or sql.is_duplicate_key_error(error) for error in errors)
    assert any(sql.is_deadlock_error(error) for error in errors) or any(
        sql.is_duplicate_key_error(error) for error in errors
    )
