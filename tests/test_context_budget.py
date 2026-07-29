import pytest

from features.ai.context_budget import (
    ContextBudgetExceededError,
    enforce_messages_context_budget,
)


def test_hard_gate_accepts_request_without_changing_messages():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "request"},
    ]

    result = enforce_messages_context_budget(
        messages,
        token_limit=100,
        max_output_tokens=0,
        safety_tokens=0,
        model=None,
    )

    assert result.messages == messages
    assert result.request_tokens <= 100


def test_hard_gate_rejects_without_truncating_tool_result():
    tool_content = "x" * 10_000
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "current request"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "tool", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "name": "tool",
            "content": tool_content,
        },
    ]
    with pytest.raises(ContextBudgetExceededError):
        enforce_messages_context_budget(
            messages,
            token_limit=100,
            max_output_tokens=0,
            safety_tokens=0,
            model=None,
        )

    assert messages[-1]["content"] == tool_content


def test_hard_gate_counts_output_safety_and_tool_schema_reserves():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "request"},
    ]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "tool",
                "description": "description",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]

    with pytest.raises(ContextBudgetExceededError):
        enforce_messages_context_budget(
            messages,
            token_limit=10,
            max_output_tokens=4,
            safety_tokens=2,
            model=None,
            tools=tools,
        )
