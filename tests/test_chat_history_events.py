import asyncio
import json
from contextlib import asynccontextmanager

from core import mysql_connection


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

    monkeypatch.setattr(mysql_connection, "transaction", fake_transaction)
    monkeypatch.setattr(mysql_connection, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(mysql_connection, "estimate_conversation_tokens", lambda *args, **kwargs: 1)

    asyncio.run(
        mysql_connection.insert_chat_records(
            123,
            [("user", '<metadata event="command"><message>/clear</message></metadata>')],
        )
    )

    stored_messages = json.loads(executed[-1][1][1])
    assert stored_messages[0]["role"] == "user"
    assert "/clear" in stored_messages[0]["content"]
    assert 'history_state="new_session"' in stored_messages[1]["content"]
