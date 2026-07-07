import pytest

from core import config
from features.ai import litellm_client
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
