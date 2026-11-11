import asyncio
from types import SimpleNamespace

import pytest

from fogmoe_telegram_bot.core.deadline import Deadline
from fogmoe_telegram_bot.features.ai import tool_runner
from fogmoe_telegram_bot.features.ai.tool_history import (
    tool_logs_completed_clear,
    turn_messages_to_record_entries,
)
from fogmoe_telegram_bot.features.ai.types import (
    TOOL_CONTEXT_MESSAGES_KEY,
    LoopStatus,
    TurnDeadlineError,
)


def test_visible_content_sent_with_a_tool_call_is_recorded_once():
    entries = turn_messages_to_record_entries(
        [
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
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "name": "google_search",
                "content": '{"ok": true}',
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


def test_standalone_visible_content_is_recorded():
    entries = turn_messages_to_record_entries(
        [{"role": "assistant", "content": "这是已经发出的最终回复。"}]
    )

    assert entries == [("assistant", "这是已经发出的最终回复。")]


def test_telegram_events_are_recorded_after_the_tool_result():
    entries = turn_messages_to_record_entries(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "execute_telegram_command",
                            "arguments": '{"command": "/me"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "name": "execute_telegram_command",
                "content": '{"success": true}',
            },
            {"role": "user", "content": "command-event"},
            {"role": "user", "content": "reply-event"},
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


def test_turn_messages_record_visible_text_tool_pairs_and_telegram_events(monkeypatch):
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
    entries = turn_messages_to_record_entries(result.turn_messages)
    assert [role for role, _ in entries] == ["assistant", "tool", "user", "assistant"]


def test_interrupted_round_turn_messages_stay_paired(monkeypatch):
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
    entries = turn_messages_to_record_entries(err.turn_messages)
    assert [role for role, _ in entries] == ["assistant", "tool", "tool"]
    assert [entry["tool_call_id"] for role, entry in entries if role == "tool"] == [
        "call_a",
        "call_b",
    ]
