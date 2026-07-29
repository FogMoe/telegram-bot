from features.ai.tool_history import (
    tool_logs_completed_clear,
    tool_logs_to_record_entries,
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
