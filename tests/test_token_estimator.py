from core.token_estimator import (
    DEFAULT_MESSAGE_OVERHEAD,
    estimate_message_tokens,
    estimate_message_tokens_raw,
    estimate_tokens,
    estimate_tokens_raw,
)


def test_estimate_tokens_raw_weights_ascii_cjk_and_other_text():
    assert estimate_tokens_raw("abc") == 1.0
    assert estimate_tokens_raw("你") == 1.1
    assert estimate_tokens_raw("🙂") == 1.8


def test_estimate_tokens_applies_guard_ratio_and_rounding():
    assert estimate_tokens("abc", guard_ratio=None) == 1
    assert estimate_tokens("abc", guard_ratio=1.15) == 2


def test_estimate_message_tokens_includes_message_overhead_and_content():
    messages = [{"role": "user", "content": "abc"}]

    assert estimate_message_tokens_raw(messages) == DEFAULT_MESSAGE_OVERHEAD + 1.0
    assert estimate_message_tokens(messages, guard_ratio=None) == 5


def test_estimate_message_tokens_can_ignore_tool_calls():
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"function": {"name": "get_help_text", "arguments": "{}"}},
            ],
        }
    ]

    with_tools = estimate_message_tokens_raw(messages, include_tool_calls=True)
    without_tools = estimate_message_tokens_raw(messages, include_tool_calls=False)

    assert with_tools > without_tools
    assert without_tools == DEFAULT_MESSAGE_OVERHEAD
