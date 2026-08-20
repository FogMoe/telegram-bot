"""锚定 core 与对话业务之间的历史回调时序。

`core.telegram_history` 写库后只发信号，摘要生成与归档的先后顺序由
`features.conversation.history_hooks` 兑现。这里把该时序钉住，避免后续
重构悄悄改掉溢出时的 summary 行为。
"""

import asyncio

import pytest

from core import archive_utils, mysql_connection, telegram_history
from features.ai import summary


@pytest.fixture(autouse=True)
def _clear_pending_history_events():
    telegram_history._PENDING_EVENTS.clear()
    yield
    telegram_history._PENDING_EVENTS.clear()


def _run_write(monkeypatch, *, insert_result, operations):
    async def fake_insert(conversation_id, records):
        operations.append(("insert", conversation_id, records))
        return insert_result

    async def fake_archive(bot, user_id, records, logger=None):
        operations.append(("archive", user_id, records))

    monkeypatch.setattr(
        mysql_connection,
        "async_insert_chat_records",
        fake_insert,
    )
    monkeypatch.setattr(
        archive_utils,
        "send_permanent_records_archive",
        fake_archive,
    )

    events = [telegram_history._PendingTelegramEvent(content="hi", bot=object())]
    asyncio.run(telegram_history._write_pending_events(123, events))


def test_overflow_writes_immediate_summary_before_archiving(monkeypatch):
    operations = []

    async def fake_generate(user_id):
        operations.append(("generate_summary", user_id))
        return "摘要"

    async def fake_update(user_id, text):
        operations.append(("store_summary", user_id, text))

    monkeypatch.setattr(summary, "generate_summary_immediately", fake_generate)
    monkeypatch.setattr(
        summary,
        "schedule_summary_generation",
        lambda user_id: operations.append(("schedule_summary", user_id)),
    )
    monkeypatch.setattr(
        mysql_connection,
        "async_update_latest_history_state_summary",
        fake_update,
    )

    _run_write(
        monkeypatch,
        insert_result=(True, "overflow", [("user", "archived")]),
        operations=operations,
    )

    assert [operation[0] for operation in operations] == [
        "insert",
        "generate_summary",
        "store_summary",
        "archive",
    ]


def test_overflow_falls_back_to_background_summary(monkeypatch):
    operations = []

    async def fake_generate(user_id):
        operations.append(("generate_summary", user_id))
        return None

    monkeypatch.setattr(summary, "generate_summary_immediately", fake_generate)
    monkeypatch.setattr(
        summary,
        "schedule_summary_generation",
        lambda user_id: operations.append(("schedule_summary", user_id)),
    )

    _run_write(
        monkeypatch,
        insert_result=(True, "overflow", []),
        operations=operations,
    )

    assert [operation[0] for operation in operations] == [
        "insert",
        "generate_summary",
        "schedule_summary",
    ]


def test_new_snapshot_only_schedules_background_summary(monkeypatch):
    operations = []

    async def fail_generate(user_id):
        raise AssertionError("新快照不应触发即时摘要")

    monkeypatch.setattr(summary, "generate_summary_immediately", fail_generate)
    monkeypatch.setattr(
        summary,
        "schedule_summary_generation",
        lambda user_id: operations.append(("schedule_summary", user_id)),
    )

    _run_write(
        monkeypatch,
        insert_result=(True, None, []),
        operations=operations,
    )

    assert [operation[0] for operation in operations] == ["insert", "schedule_summary"]


def test_plain_write_does_not_touch_summary(monkeypatch):
    operations = []

    async def fail_generate(user_id):
        raise AssertionError("普通写入不应触发摘要")

    monkeypatch.setattr(summary, "generate_summary_immediately", fail_generate)
    monkeypatch.setattr(
        summary,
        "schedule_summary_generation",
        lambda user_id: operations.append(("schedule_summary", user_id)),
    )

    _run_write(
        monkeypatch,
        insert_result=(False, None, []),
        operations=operations,
    )

    assert [operation[0] for operation in operations] == ["insert"]
