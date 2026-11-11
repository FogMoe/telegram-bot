import asyncio
from types import SimpleNamespace

import pytest

from fogmoe_telegram_bot.core.deadline import Deadline
from fogmoe_telegram_bot.features.ai import tool_runner
from fogmoe_telegram_bot.features.ai.tool_history import (
    tool_logs_completed_clear,
    tool_logs_to_record_entries,
    turn_messages_to_record_entries,
)
from fogmoe_telegram_bot.features.ai.types import (
    TOOL_CONTEXT_MESSAGES_KEY,
    LoopStatus,
    TurnDeadlineError,
)


def test_tool_logs_to_record_entries_deduplicates_visible_tool_call_content():
    entries = tool_logs_to_record_entries(
        [
            {
                "type": "assistant_visible",
                "content": "我先查一下。",
            },
            {
                "type": "assistant_tool_call",
                "tool_name": "google_search",
                "arguments": {"query": "example"},
                "tool_call_id": "call_1",
                "assistant_message": {
                    "role": "assistant",
                    "content": "我先查一下。",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "google_search",
                                "arguments": '{"query": "example"}',
                            },
                        }
                    ],
                },
            },
            {
                "type": "tool_result",
                "tool_name": "google_search",
                "arguments": {"query": "example"},
                "result": {"ok": True},
                "tool_call_id": "call_1",
            },
        ]
    )

    assert entries == [
        (
            "assistant",
            {
                "role": "assistant",
                "content": "我先查一下。",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "google_search",
                            "arguments": '{"query": "example"}',
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
                "name": "google_search",
                "content": '{"ok": true}',
            },
        ),
    ]


def test_tool_logs_to_record_entries_keeps_standalone_visible_content():
    entries = tool_logs_to_record_entries(
        [
            {
                "type": "assistant_visible",
                "content": "这是已经发出的最终回复。",
            }
        ]
    )

    assert entries == [("assistant", "这是已经发出的最终回复。")]


def test_tool_logs_to_record_entries_keeps_telegram_events_after_tool_result():
    entries = tool_logs_to_record_entries(
        [
            {
                "type": "assistant_tool_call",
                "tool_name": "execute_telegram_command",
                "arguments": {"command": "/me"},
                "tool_call_id": "call_1",
            },
            {
                "type": "tool_result",
                "tool_name": "execute_telegram_command",
                "arguments": {"command": "/me"},
                "result": {"success": True},
                "tool_call_id": "call_1",
            },
            {
                "type": "telegram_event",
                "role": "user",
                "content": "command-event",
            },
            {
                "type": "telegram_event",
                "role": "user",
                "content": "reply-event",
            },
        ]
    )

    assert [role for role, _ in entries] == ["assistant", "tool", "user", "user"]
    assert entries[-2:] == [("user", "command-event"), ("user", "reply-event")]


def test_tool_logs_completed_clear_requires_successful_clear_result():
    successful_clear = {
        "type": "tool_result",
        "tool_name": "execute_telegram_command",
        "arguments": {"command": "/clear"},
        "result": {"success": True},
    }

    assert tool_logs_completed_clear([successful_clear]) is True
    assert tool_logs_completed_clear(
        [{**successful_clear, "result": {"success": False}}]
    ) is False
    assert tool_logs_completed_clear(
        [{**successful_clear, "arguments": {"command": "/me"}}]
    ) is False


def test_turn_messages_and_tool_logs_produce_identical_history_entries(monkeypatch):
    responses = [
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="我先查一下。",
                        tool_calls=[
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "execute_telegram_command",
                                    "arguments": '{"command": "/me"}',
                                },
                            }
                        ],
                    )
                )
            ]
        ),
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="查询完成啦。", tool_calls=None)
                )
            ]
        ),
    ]

    async def fake_completion(*args, **kwargs):
        return responses.pop(0)

    async def fake_visible(text: str) -> str:
        return text

    monkeypatch.setattr(tool_runner, "create_chat_completion", fake_completion)

    result = asyncio.run(
        tool_runner.run_tool_loop(
            "openai",
            "gpt-4o",
            [{"role": "user", "content": "看下我的资料"}],
            visible_content_handler=fake_visible,
            tool_definitions=[
                {
                    "type": "function",
                    "function": {"name": "execute_telegram_command", "parameters": {}},
                }
            ],
            tool_handlers={
                "execute_telegram_command": lambda command: {
                    "success": True,
                    TOOL_CONTEXT_MESSAGES_KEY: ["<metadata event=\"command\" />"],
                }
            },
        )
    )

    assert result.status is LoopStatus.COMPLETED
    assert result.turn_messages is not None
    from_messages = turn_messages_to_record_entries(result.turn_messages)
    from_logs = tool_logs_to_record_entries(result.tool_logs)
    assert from_messages == from_logs
    assert [role for role, _ in from_messages] == ["assistant", "tool", "user", "assistant"]


def test_interrupted_round_turn_messages_stay_paired_with_tool_logs(monkeypatch):
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content="",
                    tool_calls=[
                        {
                            "id": "call_a",
                            "type": "function",
                            "function": {"name": "slow_tool", "arguments": "{}"},
                        },
                        {
                            "id": "call_b",
                            "type": "function",
                            "function": {"name": "fast_tool", "arguments": "{}"},
                        },
                    ],
                )
            )
        ]
    )

    async def fake_completion(*args, **kwargs):
        return response

    async def slow_tool():
        await asyncio.sleep(5)
        return {"ok": True}

    monkeypatch.setattr(tool_runner, "create_chat_completion", fake_completion)

    with pytest.raises(TurnDeadlineError) as exc_info:
        asyncio.run(
            tool_runner.run_tool_loop(
                "openai",
                "gpt-4o",
                [{"role": "user", "content": "run"}],
                tool_definitions=[
                    {"type": "function", "function": {"name": "slow_tool", "parameters": {}}},
                    {"type": "function", "function": {"name": "fast_tool", "parameters": {}}},
                ],
                tool_handlers={"slow_tool": slow_tool, "fast_tool": lambda: {"ok": True}},
                deadline=Deadline(0.05),
            )
        )

    err = exc_info.value
    assert err.turn_messages is not None
    assert turn_messages_to_record_entries(err.turn_messages) == tool_logs_to_record_entries(
        err.tool_logs
    )
