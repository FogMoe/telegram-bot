"""数据库工具在原生 async 工具循环里对真实 MySQL 的行为：直接 await，不经 run_sync，也不占线程。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from economy_support import seed_user
from mysql_support import execute, fetch, run

from fogmoe_telegram_bot.core import mysql_connection
from fogmoe_telegram_bot.features.ai import tool_runner
from fogmoe_telegram_bot.features.ai.tools import context as tool_context


def _call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _scripted_model(*responses):
    queue = list(responses)

    async def fake_create_chat_completion(provider, model, *, messages, **kwargs):
        return queue.pop(0)

    return fake_create_chat_completion


def _forbid_run_sync(monkeypatch):
    def forbidden(coro):
        coro.close()
        raise AssertionError("the tool loop must not bridge through run_sync")

    monkeypatch.setattr(mysql_connection, "run_sync", forbidden)


def _response(content="", tool_calls=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def test_diary_and_schedule_tools_write_to_the_real_database_inside_the_tool_loop(
    app_database, monkeypatch
):
    seed_user(app_database, 7, free=10)
    _forbid_run_sync(monkeypatch)
    run_at = (datetime.now(UTC) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(
        tool_runner,
        "create_chat_completion",
        _scripted_model(
            _response(
                tool_calls=[
                    _call(
                        "c1",
                        "user_diary",
                        {
                            "action": "append",
                            "page": 1,
                            "title": "笔记",
                            "summary": "日常记录",
                            "content": "今天聊了很多",
                        },
                    ),
                    _call(
                        "c2",
                        "schedule_ai_message",
                        {
                            "action": "create",
                            "timestamp_utc": run_at,
                            "trigger_reason": "测试提醒",
                            "instruction": "说一声你好",
                        },
                    ),
                ]
            ),
            _response("记下了"),
        ),
    )

    async def scenario():
        tool_context.set_tool_request_context({"user_id": 7})
        return await tool_runner.run_tool_loop(
            "openai",
            "test-model",
            [{"role": "user", "content": "记一下"}],
            {"user_id": 7},
            provider_name="Test",
        )

    message, tool_logs = run(scenario())

    assert message == "记下了"
    results = {log["tool_name"]: log["result"] for log in tool_logs if log["type"] == "tool_result"}
    assert results["user_diary"]["total_pages"] == 1
    assert results["schedule_ai_message"]["status"] == "scheduled"

    diary = fetch(
        app_database,
        "SELECT page_no, title, summary, content FROM ai_user_diary_pages WHERE user_id = 7",
    )
    assert diary == [{"page_no": 1, "title": "笔记", "summary": "日常记录", "content": "今天聊了很多"}]
    schedules = fetch(app_database, "SELECT user_id, status, prompt FROM ai_schedules")
    assert schedules == [{"user_id": 7, "status": "pending", "prompt": "说一声你好"}]


def test_reading_tools_return_what_the_writing_tools_stored(app_database, monkeypatch):
    seed_user(app_database, 7, free=10)
    _forbid_run_sync(monkeypatch)
    execute(
        app_database,
        (
            "INSERT INTO ai_user_diary_pages (user_id, page_no, title, summary, content) "
            "VALUES (%s, %s, %s, %s, %s)",
            (7, 1, "旧笔记", "之前写的", "第一行\n第二行"),
        ),
    )
    monkeypatch.setattr(
        tool_runner,
        "create_chat_completion",
        _scripted_model(
            _response(
                tool_calls=[
                    _call("c1", "user_diary", {"action": "read", "page": 1}),
                    _call("c2", "user_diary", {"action": "index"}),
                ]
            ),
            _response("看过了"),
        ),
    )

    async def scenario():
        tool_context.set_tool_request_context({"user_id": 7})
        return await tool_runner.run_tool_loop(
            "openai",
            "test-model",
            [{"role": "user", "content": "看一下日记"}],
            {"user_id": 7},
            provider_name="Test",
        )

    message, tool_logs = run(scenario())

    assert message == "看过了"
    results = [log["result"] for log in tool_logs if log["type"] == "tool_result"]
    assert results[0]["content"] == "第一行\n第二行" and results[0]["total_lines"] == 2
    assert [page["title"] for page in results[1]["pages"]] == ["旧笔记"]
