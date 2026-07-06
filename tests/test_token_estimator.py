from core import mysql_connection, token_estimator
from core.token_estimator import (
    DEFAULT_MESSAGE_OVERHEAD,
    estimate_conversation_tokens,
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


def test_estimate_message_tokens_prefers_litellm_when_model_is_available(monkeypatch):
    messages = [{"role": "user", "content": "abc"}]
    recorded = {}

    def fake_token_counter(**kwargs):
        recorded.update(kwargs)
        return 10

    monkeypatch.setattr(token_estimator.litellm, "token_counter", fake_token_counter)

    assert estimate_message_tokens(messages, model="openai/gpt-4o", guard_ratio=None) == 10

    assert recorded["model"] == "openai/gpt-4o"
    assert recorded["messages"] == messages


def test_estimate_message_tokens_applies_guard_to_litellm_count(monkeypatch):
    def fake_token_counter(**kwargs):
        return 10

    monkeypatch.setattr(token_estimator.litellm, "token_counter", fake_token_counter)

    assert estimate_message_tokens(
        [{"role": "user", "content": "abc"}],
        model="openai/gpt-4o",
        guard_ratio=1.15,
    ) == 12


def test_estimate_message_tokens_falls_back_when_litellm_fails(monkeypatch):
    messages = [{"role": "user", "content": "abc"}]

    def fake_token_counter(**kwargs):
        raise RuntimeError("unsupported model")

    monkeypatch.setattr(token_estimator.litellm, "token_counter", fake_token_counter)

    assert estimate_message_tokens(messages, model="bad/model", guard_ratio=None) == 5


def test_estimate_conversation_tokens_counts_system_prompt_with_litellm(monkeypatch):
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"function": {"name": "get_help_text", "arguments": "{}"}},
            ],
        }
    ]
    recorded = {}

    def fake_token_counter(**kwargs):
        recorded.update(kwargs)
        return 9

    monkeypatch.setattr(token_estimator.litellm, "token_counter", fake_token_counter)

    assert estimate_conversation_tokens(
        messages,
        system_prompt="base",
        system_prompt_extra="extra",
        include_tool_calls=False,
        model="openai/gpt-4o",
        guard_ratio=None,
    ) == 9

    assert recorded["messages"][0] == {"role": "system", "content": "baseextra"}
    assert "tool_calls" not in recorded["messages"][1]


def test_chat_token_count_model_uses_first_configured_chat_model(monkeypatch):
    monkeypatch.setattr(mysql_connection.config, "AI_SERVICE_ORDER", ["openai", "gemini"])
    monkeypatch.setattr(mysql_connection.config, "OPENAI_CHAT_MODEL", None)
    monkeypatch.setattr(mysql_connection.config, "GEMINI_CHAT_MODEL", "gemini-2.5-pro")
    monkeypatch.setattr(mysql_connection.config, "GEMINI_CHAT_FALLBACK_MODEL", None)
    monkeypatch.setattr(
        mysql_connection,
        "litellm_model_name",
        lambda provider, model: f"{provider}/{model}",
    )

    assert mysql_connection._chat_token_count_model() == "gemini/gemini-2.5-pro"
