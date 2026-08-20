import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

from core import chat_records


def test_new_session_event_follows_first_user_action(monkeypatch):
    executed = []

    class FakeConnection:
        async def exec_driver_sql(self, sql, params):
            executed.append((sql, params))

    @asynccontextmanager
    async def fake_transaction():
        yield FakeConnection()

    async def fake_fetch_one(*args, **kwargs):
        return None

    monkeypatch.setattr(chat_records, "transaction", fake_transaction)
    monkeypatch.setattr(chat_records, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(chat_records, "estimate_conversation_tokens", lambda *args, **kwargs: 1)

    asyncio.run(
        chat_records.insert_chat_records(
            123,
            [("user", '<metadata><message>hello</message></metadata>')],
        )
    )

    stored_messages = json.loads(executed[-1][1][1])
    assert stored_messages[0]["role"] == "user"
    assert "hello" in stored_messages[0]["content"]
    assert 'history_state="new_session"' in stored_messages[1]["content"]


def test_clear_archives_final_records_and_leaves_only_new_session(monkeypatch):
    executed = []

    class FakeConnection:
        async def exec_driver_sql(self, sql, params):
            executed.append((sql, params))
            return SimpleNamespace(lastrowid=77)

    @asynccontextmanager
    async def fake_transaction():
        yield FakeConnection()

    async def fake_fetch_one(sql, *args, **kwargs):
        if sql.startswith("SELECT messages FROM chat_records"):
            return ('[{"role":"user","content":"old-message"}]',)
        return None

    async def fake_prune(*args, **kwargs):
        return []

    monkeypatch.setattr(chat_records, "transaction", fake_transaction)
    monkeypatch.setattr(chat_records, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(chat_records, "prune_permanent_records", fake_prune)

    record_id, archived_records = asyncio.run(
        chat_records.archive_chat_and_start_new_session(
            123,
            [
                (
                    "assistant",
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "execute_telegram_command",
                                    "arguments": '{"command":"/clear"}',
                                },
                            }
                        ],
                    },
                ),
                (
                    "tool",
                    {
                        "role": "tool",
                        "tool_call_id": "call_1",
                        "name": "execute_telegram_command",
                        "content": '{"success":true}',
                    },
                ),
                ("user", "clear-event"),
                ("user", "reply-event"),
            ],
        )
    )

    archive_insert = next(
        params
        for sql, params in executed
        if sql.startswith("INSERT INTO permanent_chat_records")
    )
    archived_messages = json.loads(archive_insert[1])
    active_update = next(
        params
        for sql, params in executed
        if sql.startswith("UPDATE chat_records")
    )
    active_messages = json.loads(active_update[0])

    assert record_id == 77
    assert archived_records == []
    assert [message["role"] for message in archived_messages] == [
        "user",
        "assistant",
        "tool",
        "user",
        "user",
    ]
    assert archived_messages[0]["content"] == "old-message"
    assert archived_messages[1]["tool_calls"][0]["id"] == "call_1"
    assert archived_messages[2]["content"] == '{"success":true}'
    assert archived_messages[3]["content"] == "clear-event"
    assert archived_messages[4]["content"] == "reply-event"
    assert len(active_messages) == 1
    assert 'history_state="new_session"' in active_messages[0]["content"]


def test_clear_keeps_zero_balance_history_suspended(monkeypatch):
    executed = []
    suspended = chat_records._build_coin_service_state_event("suspended")

    class FakeConnection:
        async def exec_driver_sql(self, sql, params):
            executed.append((sql, params))
            return SimpleNamespace(lastrowid=77)

    @asynccontextmanager
    async def fake_transaction():
        yield FakeConnection()

    async def fake_fetch_one(sql, *args, **kwargs):
        if sql.startswith("SELECT messages FROM chat_records"):
            return (json.dumps([suspended]),)
        if sql.startswith("SELECT coins, coins_paid FROM user"):
            return (0, 0)
        return None

    async def fake_prune(*args, **kwargs):
        return []

    monkeypatch.setattr(chat_records, "transaction", fake_transaction)
    monkeypatch.setattr(chat_records, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(chat_records, "prune_permanent_records", fake_prune)

    asyncio.run(chat_records.archive_chat_and_start_new_session(123, []))

    active_update = next(
        params
        for sql, params in executed
        if sql.startswith("UPDATE chat_records")
    )
    active_messages = json.loads(active_update[0])
    assert 'history_state="new_session"' in active_messages[0]["content"]
    assert 'service_state="suspended"' in active_messages[1]["content"]


def test_append_permanent_chat_record_extends_the_clear_archive(monkeypatch):
    executed = []

    class FakeConnection:
        async def exec_driver_sql(self, sql, params):
            executed.append((sql, params))
            return SimpleNamespace(lastrowid=None)

    @asynccontextmanager
    async def fake_transaction():
        yield FakeConnection()

    async def fake_fetch_one(*args, **kwargs):
        return ('[{"role":"user","content":"clear-event"}]',)

    monkeypatch.setattr(chat_records, "transaction", fake_transaction)
    monkeypatch.setattr(chat_records, "fetch_one", fake_fetch_one)

    asyncio.run(
        chat_records.append_permanent_chat_record(
            123,
            77,
            [("user", "reply-event")],
        )
    )

    update_params = executed[-1][1]
    assert [message["content"] for message in json.loads(update_params[0])] == [
        "clear-event",
        "reply-event",
    ]
    assert update_params[1:] == (77, 123)


def _run_history_insert(
    monkeypatch,
    *,
    existing_messages,
    coin_balances,
    records,
    **insert_kwargs,
):
    executed = []

    class FakeConnection:
        async def exec_driver_sql(self, sql, params):
            executed.append((sql, params))
            return SimpleNamespace(lastrowid=None)

    @asynccontextmanager
    async def fake_transaction():
        yield FakeConnection()

    async def fake_fetch_one(sql, *args, **kwargs):
        if sql.startswith("SELECT messages FROM chat_records"):
            if existing_messages is None:
                return None
            return (json.dumps(existing_messages, ensure_ascii=False),)
        if sql.startswith("SELECT coins, coins_paid FROM user"):
            return coin_balances
        return None

    monkeypatch.setattr(chat_records, "transaction", fake_transaction)
    monkeypatch.setattr(chat_records, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(
        chat_records,
        "estimate_conversation_tokens",
        lambda *args, **kwargs: 1,
    )

    result = asyncio.run(
        chat_records.insert_chat_records(
            123,
            records,
            **insert_kwargs,
        )
    )
    if not executed:
        return result, []

    sql, params = executed[-1]
    payload = params[0] if sql.startswith("UPDATE chat_records") else params[1]
    return result, json.loads(payload)


def test_zero_balance_writes_terminal_action_then_suspends_history(monkeypatch):
    _, messages = _run_history_insert(
        monkeypatch,
        existing_messages=[{"role": "user", "content": "before"}],
        coin_balances=(0, 0),
        records=[("user", "final non-chat action")],
    )

    assert [message["content"] for message in messages[:2]] == [
        "before",
        "final non-chat action",
    ]
    assert 'service_state="suspended"' in messages[2]["content"]


def test_suspended_zero_balance_drops_all_later_history(monkeypatch):
    suspended = chat_records._build_coin_service_state_event("suspended")

    result, messages = _run_history_insert(
        monkeypatch,
        existing_messages=[{"role": "user", "content": "before"}, suspended],
        coin_balances=(0, 0),
        records=[("user", "must not be stored")],
    )

    assert result == (False, None, [])
    assert messages == []


def test_positive_balance_writes_resume_marker_before_new_action(monkeypatch):
    suspended = chat_records._build_coin_service_state_event("suspended")

    _, messages = _run_history_insert(
        monkeypatch,
        existing_messages=[{"role": "user", "content": "before"}, suspended],
        coin_balances=(1, 0),
        records=[
            ("user", "coin earning command"),
            ("user", "coin earning result"),
        ],
    )

    assert 'service_state="resumed"' in messages[2]["content"]
    assert [message["content"] for message in messages[3:]] == [
        "coin earning command",
        "coin earning result",
    ]


def test_authorized_last_chat_turn_is_saved_before_suspension(monkeypatch):
    _, paid_turn_messages = _run_history_insert(
        monkeypatch,
        existing_messages=[{"role": "user", "content": "before"}],
        coin_balances=(0, 0),
        records=[("assistant", "last paid reply")],
        allow_zero_balance=True,
    )

    assert paid_turn_messages[-1]["content"] == "last paid reply"
    assert not any(
        'service_state="suspended"' in message.get("content", "")
        for message in paid_turn_messages
    )

    _, finalized_messages = _run_history_insert(
        monkeypatch,
        existing_messages=paid_turn_messages,
        coin_balances=(0, 0),
        records=[],
        suspend_if_zero=True,
    )

    assert finalized_messages[-2]["content"] == "last paid reply"
    assert 'service_state="suspended"' in finalized_messages[-1]["content"]
