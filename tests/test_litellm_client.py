import pytest
from litellm.llms.custom_httpx.http_handler import HTTPHandler

from core import config
from core.litellm_models import litellm_model_name
from features.ai import context_budget, litellm_client
from features.ai.litellm_message_sanitizer import sanitize_message_for_provider
from features.ai.litellm_provider_config import (
    azure_api_base,
    gemini_native_api_base,
    openai_compatible_api_base,
    provider_params,
)


def test_openai_compatible_api_base_strips_chat_completions_suffix():
    assert (
        openai_compatible_api_base("https://example.test/v1/chat/completions/")
        == "https://example.test/v1"
    )


def test_litellm_model_name_uses_native_openrouter_prefix():
    assert (
        litellm_model_name("openrouter", "openai/gpt-5.6-luna")
        == "openrouter/openai/gpt-5.6-luna"
    )


def test_litellm_model_name_preserves_fogmoe_endpoint_model_id():
    assert (
        litellm_model_name("fogmoe", "openai/gpt-5.6-luna")
        == "openai/openai/gpt-5.6-luna"
    )


def test_gemini_native_api_base_strips_models_suffix():
    assert (
        gemini_native_api_base("https://generativelanguage.googleapis.com/v1beta/models")
        == "https://generativelanguage.googleapis.com/v1beta"
    )


def test_azure_api_base_prefers_endpoint_over_deployment_base(monkeypatch):
    monkeypatch.setattr(config, "AZURE_OPENAI_API_ENDPOINT", "https://azure.test/")
    monkeypatch.setattr(
        config,
        "AZURE_OPENAI_BASE_URL",
        "https://ignored.test/openai/deployments/deployment",
    )

    assert azure_api_base() == "https://azure.test"


def test_azure_api_base_extracts_resource_from_deployment_base(monkeypatch):
    monkeypatch.setattr(config, "AZURE_OPENAI_API_ENDPOINT", None)
    monkeypatch.setattr(
        config,
        "AZURE_OPENAI_BASE_URL",
        "https://azure.test/openai/deployments/deployment",
    )

    assert azure_api_base() == "https://azure.test"


def test_provider_params_uses_dummy_openai_key_for_custom_base(monkeypatch):
    monkeypatch.setattr(config, "OPENAI_API_KEY", None)
    monkeypatch.setattr(config, "OPENAI_BASE_URL", "https://openai-compatible.test/v1")

    assert provider_params("openai") == {
        "api_key": "sk-no-key-required",
        "api_base": "https://openai-compatible.test/v1",
    }


def test_provider_params_requires_openai_key_or_base(monkeypatch):
    monkeypatch.setattr(config, "OPENAI_API_KEY", None)
    monkeypatch.setattr(config, "OPENAI_BASE_URL", None)

    with pytest.raises(RuntimeError, match="Missing OPENAI_API_KEY"):
        provider_params("openai")


def test_provider_params_builds_openrouter_params(monkeypatch):
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "openrouter-key")
    monkeypatch.setattr(
        config,
        "OPENROUTER_API_BASE",
        "https://openrouter.test/api/v1/chat/completions",
    )

    assert provider_params("openrouter") == {
        "api_key": "openrouter-key",
        "api_base": "https://openrouter.test/api/v1",
    }


def test_provider_params_builds_fogmoe_openai_compatible_params(monkeypatch):
    monkeypatch.setattr(config, "FOGMOE_API_KEY", "fogmoe-key")
    monkeypatch.setattr(
        config,
        "FOGMOE_API_BASE",
        "https://ai.fog.test/v1/chat/completions",
    )

    assert provider_params("fogmoe") == {
        "api_key": "fogmoe-key",
        "api_base": "https://ai.fog.test/v1",
    }


@pytest.mark.parametrize(
    ("provider", "key_name", "base_name"),
    [
        ("openrouter", "OPENROUTER_API_KEY", "OPENROUTER_API_BASE"),
        ("fogmoe", "FOGMOE_API_KEY", "FOGMOE_API_BASE"),
    ],
)
def test_new_provider_params_require_key_and_base(
    monkeypatch,
    provider,
    key_name,
    base_name,
):
    monkeypatch.setattr(config, key_name, None)
    monkeypatch.setattr(config, base_name, "https://example.test/v1")
    with pytest.raises(RuntimeError, match=f"Missing {key_name}"):
        provider_params(provider)

    monkeypatch.setattr(config, key_name, "test-key")
    monkeypatch.setattr(config, base_name, None)
    with pytest.raises(RuntimeError, match=f"Missing {base_name}"):
        provider_params(provider)


def test_provider_params_requires_gemini_base_for_openai_compatible_mode(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "gemini-key")
    monkeypatch.setattr(config, "GEMINI_OPENAI_COMPATIBLE", True)
    monkeypatch.setattr(config, "GEMINI_API_BASE", None)

    with pytest.raises(RuntimeError, match="GEMINI_OPENAI_COMPATIBLE requires"):
        provider_params("gemini")


def test_provider_params_builds_native_gemini_base(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "gemini-key")
    monkeypatch.setattr(config, "GEMINI_OPENAI_COMPATIBLE", False)
    monkeypatch.setattr(
        config,
        "GEMINI_API_BASE",
        "https://generativelanguage.googleapis.com/v1beta/models",
    )

    assert provider_params("gemini") == {
        "api_key": "gemini-key",
        "api_base": "https://generativelanguage.googleapis.com/v1beta",
    }


def test_provider_params_builds_azure_params(monkeypatch):
    monkeypatch.setattr(config, "AZURE_OPENAI_API_KEY", "azure-key")
    monkeypatch.setattr(config, "AZURE_OPENAI_API_ENDPOINT", "https://azure.test/")
    monkeypatch.setattr(config, "AZURE_OPENAI_BASE_URL", "")
    monkeypatch.setattr(config, "AZURE_OPENAI_API_VERSION", "2024-12-01-preview")

    assert provider_params("azure") == {
        "api_key": "azure-key",
        "api_base": "https://azure.test",
        "api_version": "2024-12-01-preview",
    }


def test_sanitize_message_removes_provider_specific_fields_for_non_gemini():
    message = {
        "role": "assistant",
        "content": "",
        "provider_specific_fields": {"x": 1},
        "tool_calls": [
            {
                "id": "call-1",
                "provider_specific_fields": {"x": 1},
                "function": {"name": "tool"},
            }
        ],
    }

    assert sanitize_message_for_provider(message, "openai") == {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call-1",
                "function": {"name": "tool"},
            }
        ],
    }


def test_sanitize_message_applies_native_gemini_tool_message_rules():
    assistant_message = {
        "role": "assistant",
        "content": "",
        "provider_specific_fields": {"x": 1},
        "tool_calls": [
            {
                "id": "call-1",
                "provider_specific_fields": {"x": 1},
                "function": {"name": "tool"},
            }
        ],
    }
    tool_message = {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": "result",
    }

    assert sanitize_message_for_provider(assistant_message, "gemini") == {
        "role": "assistant",
        "provider_specific_fields": {"x": 1},
        "tool_calls": [
            {
                "provider_specific_fields": {"x": 1},
                "function": {"name": "tool"},
            }
        ],
    }
    assert sanitize_message_for_provider(tool_message, "gemini") == {
        "role": "tool",
        "content": "result",
    }


def test_create_chat_completion_normalizes_provider_and_filters_none_kwargs(
    monkeypatch,
):
    calls = []
    messages = [{"role": "user", "content": "hello"}]
    monkeypatch.setattr(config, "ZAI_API_KEY", "zai-key")
    monkeypatch.setattr(config, "ZAI_API_BASE", "https://zai.test/v4")

    def fake_completion(**kwargs):
        calls.append(kwargs)
        return "ok"

    monkeypatch.setattr(litellm_client.litellm, "completion", fake_completion)

    assert (
        litellm_client.create_chat_completion(
            "zhipu",
            "glm-test",
            messages,
            temperature=None,
            max_tokens=128,
        )
        == "ok"
    )
    assert calls == [
        {
            "model": "zai/glm-test",
            "messages": messages,
            "api_key": "zai-key",
            "api_base": "https://zai.test/v4",
            "max_tokens": 128,
            "drop_params": True,
        }
    ]


@pytest.mark.parametrize(
    ("provider", "model", "expected_model", "expected_params"),
    [
        (
            "openrouter",
            "openai/gpt-5.6-luna",
            "openrouter/openai/gpt-5.6-luna",
            {
                "api_key": "openrouter-key",
                "api_base": "https://openrouter.test/api/v1",
            },
        ),
        (
            "fogmoe",
            "openai/gpt-5.6-luna",
            "openai/openai/gpt-5.6-luna",
            {
                "api_key": "fogmoe-key",
                "api_base": "https://ai.fog.test/v1",
            },
        ),
    ],
)
def test_create_chat_completion_routes_new_providers(
    monkeypatch,
    provider,
    model,
    expected_model,
    expected_params,
):
    calls = []
    monkeypatch.setattr(
        litellm_client,
        "_provider_params",
        lambda resolved_provider: expected_params,
    )
    monkeypatch.setattr(
        litellm_client.litellm,
        "completion",
        lambda **kwargs: calls.append(kwargs) or "ok",
    )

    messages = [{"role": "user", "content": "hello"}]
    assert litellm_client.create_chat_completion(provider, model, messages) == "ok"
    assert calls == [
        {
            "model": expected_model,
            "messages": messages,
            **expected_params,
            "drop_params": True,
        }
    ]


def test_create_chat_completion_uses_twenty_five_percent_hard_limit(monkeypatch):
    recorded = {}
    messages = [{"role": "user", "content": "hello"}]
    tools = [{"type": "function", "function": {"name": "lookup"}}]

    def fake_enforce(request_messages, **kwargs):
        recorded["messages"] = request_messages
        recorded.update(kwargs)
        return context_budget.ContextBudgetResult(
            messages=list(request_messages),
            request_tokens=149_999,
        )

    monkeypatch.setattr(config, "CHAT_TOKEN_LIMIT", 120_000)
    monkeypatch.setattr(config, "CHAT_CONTEXT_HARD_LIMIT_RATIO", 1.25)
    monkeypatch.setattr(
        litellm_client,
        "enforce_messages_context_budget",
        fake_enforce,
    )
    monkeypatch.setattr(litellm_client, "_provider_params", lambda provider: {})
    monkeypatch.setattr(litellm_client.litellm, "completion", lambda **kwargs: "ok")

    assert (
        litellm_client.create_chat_completion(
            "openai",
            "test-model",
            messages,
            tools=tools,
            max_tokens=4096,
        )
        == "ok"
    )
    assert recorded == {
        "messages": messages,
        "token_limit": 150_000,
        "max_output_tokens": 4096,
        "safety_tokens": config.CHAT_CONTEXT_SAFETY_TOKENS,
        "model": "test-model",
        "tools": tools,
    }


def test_create_chat_completion_accepts_summary_hard_limit_override(monkeypatch):
    recorded = {}
    provider_calls = []
    messages = [{"role": "user", "content": "summarize"}]

    def fake_enforce(request_messages, **kwargs):
        recorded.update(kwargs)
        return context_budget.ContextBudgetResult(
            messages=list(request_messages),
            request_tokens=179_999,
        )

    monkeypatch.setattr(config, "CHAT_TOKEN_LIMIT", 120_000)
    monkeypatch.setattr(config, "CHAT_CONTEXT_HARD_LIMIT_RATIO", 1.25)
    monkeypatch.setattr(
        litellm_client,
        "enforce_messages_context_budget",
        fake_enforce,
    )
    monkeypatch.setattr(litellm_client, "_provider_params", lambda provider: {})
    monkeypatch.setattr(
        litellm_client.litellm,
        "completion",
        lambda **kwargs: provider_calls.append(kwargs) or "ok",
    )

    assert (
        litellm_client.create_chat_completion(
            "openai",
            "summary-model",
            messages,
            context_hard_limit_ratio=1.5,
            max_tokens=2500,
        )
        == "ok"
    )
    assert recorded["token_limit"] == 180_000
    assert "context_hard_limit_ratio" not in provider_calls[0]


def test_create_chat_completion_blocks_before_provider_call(monkeypatch):
    provider_called = False

    def reject_request(messages, **kwargs):
        raise context_budget.ContextBudgetExceededError(150_001, 150_000)

    def fake_completion(**kwargs):
        nonlocal provider_called
        provider_called = True

    monkeypatch.setattr(
        litellm_client,
        "enforce_messages_context_budget",
        reject_request,
    )
    monkeypatch.setattr(litellm_client.litellm, "completion", fake_completion)

    with pytest.raises(context_budget.ContextBudgetExceededError):
        litellm_client.create_chat_completion(
            "openai",
            "test-model",
            [{"role": "user", "content": "oversized"}],
        )

    assert provider_called is False


def test_create_chat_completion_uses_openai_history_shape_for_compatible_gemini(
    monkeypatch,
):
    calls = []
    messages = [
        {
            "role": "assistant",
            "content": "",
            "provider_specific_fields": {"x": 1},
            "tool_calls": [
                {
                    "id": "call-1",
                    "provider_specific_fields": {"x": 1},
                    "function": {"name": "tool"},
                }
            ],
        }
    ]
    monkeypatch.setattr(config, "GEMINI_API_KEY", "gemini-key")
    monkeypatch.setattr(config, "GEMINI_OPENAI_COMPATIBLE", True)
    monkeypatch.setattr(config, "GEMINI_API_BASE", "https://gemini-compatible.test/v1")

    def fake_completion(**kwargs):
        calls.append(kwargs)
        return "ok"

    monkeypatch.setattr(litellm_client.litellm, "completion", fake_completion)

    assert litellm_client.create_chat_completion("gemini", "gemini-test", messages) == "ok"
    assert calls[0]["model"] == "openai/gemini-test"
    assert calls[0]["messages"] == [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "function": {"name": "tool"},
                }
            ],
        }
    ]


def test_gemini_native_http_handler_uses_canonical_system_instruction_key(
    monkeypatch,
):
    recorded = {}

    def fake_post(self, *args, **kwargs):
        recorded.update(kwargs)
        return "ok"

    monkeypatch.setattr(HTTPHandler, "post", fake_post)
    handler = object.__new__(litellm_client._GeminiNativeHTTPHandler)

    assert (
        handler.post(
            json={
                "system_instruction": {"parts": [{"text": "system"}]},
                "contents": [],
            }
        )
        == "ok"
    )
    assert recorded["json"] == {
        "systemInstruction": {"parts": [{"text": "system"}]},
        "contents": [],
    }


def test_create_chat_completion_uses_compat_client_for_custom_native_gemini(
    monkeypatch,
):
    calls = []
    clients = []

    class FakeCompatClient:
        def __init__(self, timeout=None):
            self.timeout = timeout
            self.closed = False
            clients.append(self)

        def close(self):
            self.closed = True

    monkeypatch.setattr(config, "GEMINI_API_KEY", "gemini-key")
    monkeypatch.setattr(config, "GEMINI_OPENAI_COMPATIBLE", False)
    monkeypatch.setattr(config, "GEMINI_API_BASE", "https://gemini-native.test/v1beta")
    monkeypatch.setattr(
        litellm_client,
        "_GeminiNativeHTTPHandler",
        FakeCompatClient,
    )
    monkeypatch.setattr(
        litellm_client.litellm,
        "completion",
        lambda **kwargs: calls.append(kwargs) or "ok",
    )

    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
    ]
    assert (
        litellm_client.create_chat_completion(
            "gemini",
            "gemini-test",
            messages,
            timeout=17,
        )
        == "ok"
    )
    assert calls[0]["client"] is clients[0]
    assert clients[0].timeout == 17
    assert clients[0].closed is True
