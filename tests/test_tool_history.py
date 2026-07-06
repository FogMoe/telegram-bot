from features.ai.tool_history import tool_logs_to_record_entries


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
