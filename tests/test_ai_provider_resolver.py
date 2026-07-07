import pytest

from core import config
from features.ai import provider_resolver, task_runner


def test_get_provider_order_for_chat_returns_configured_order_copy(monkeypatch):
    monkeypatch.setattr(config, "AI_SERVICE_ORDER", ["openai", "zhipu"])

    order = provider_resolver.get_provider_order_for_task("chat")
    order.append("gemini")

    assert order == ["openai", "zhipu", "gemini"]
    assert config.AI_SERVICE_ORDER == ["openai", "zhipu"]


def test_get_provider_order_for_subtask_uses_primary_and_fallback(monkeypatch):
    monkeypatch.setattr(config, "AI_SUMMARY_PROVIDER", " OpenAI ")
    monkeypatch.setattr(config, "AI_SUMMARY_FALLBACK_PROVIDER", " ZHIPU ")

    assert provider_resolver.get_provider_order_for_task("summary") == [
        "openai",
        "zhipu",
    ]


def test_get_provider_order_for_subtask_deduplicates_case_insensitively(monkeypatch):
    monkeypatch.setattr(config, "AI_TRANSLATE_PROVIDER", "OpenAI")
    monkeypatch.setattr(config, "AI_TRANSLATE_FALLBACK_PROVIDER", "openai")

    assert provider_resolver.get_provider_order_for_task("translate") == ["openai"]


def test_get_provider_order_for_unknown_task_fails():
    with pytest.raises(RuntimeError, match="Unsupported AI task"):
        provider_resolver.get_provider_order_for_task("embedding")


@pytest.mark.parametrize(
    ("provider", "config_name"),
    [
        ("openai", "OPENAI_VISION_MODEL"),
        ("siliconflow", "SILICONFLOW_VISION_MODEL"),
        ("gemini", "GEMINI_VISION_MODEL"),
        ("zhipu", "ZHIPU_VISION_MODEL"),
        ("zai", "ZHIPU_VISION_MODEL"),
        ("azure", "AZURE_OPENAI_VISION_MODEL"),
    ],
)
def test_get_models_for_task_uses_provider_specific_model_config(
    monkeypatch,
    provider,
    config_name,
):
    monkeypatch.setattr(config, config_name, f"{provider}-vision-model")

    assert provider_resolver.get_models_for_task(provider, "vision") == [
        f"{provider}-vision-model"
    ]


def test_get_models_for_task_includes_gemini_task_fallback(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_SUMMARY_MODEL", "gemini-summary-primary")
    monkeypatch.setattr(config, "GEMINI_SUMMARY_FALLBACK_MODEL", "gemini-summary-fallback")

    assert provider_resolver.get_models_for_task("gemini", "summary") == [
        "gemini-summary-primary",
        "gemini-summary-fallback",
    ]


def test_get_models_for_task_deduplicates_identical_primary_and_fallback(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_CHAT_MODEL", "gemini-chat")
    monkeypatch.setattr(config, "GEMINI_CHAT_FALLBACK_MODEL", "gemini-chat")

    assert provider_resolver.get_models_for_task("gemini", "chat") == ["gemini-chat"]


def test_completion_kwargs_adds_reasoning_effort_for_native_gemini(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_OPENAI_COMPATIBLE", False)

    assert provider_resolver.completion_kwargs_for_task("gemini", "vision") == {
        "reasoning_effort": "high"
    }
    assert provider_resolver.completion_kwargs_for_task("gemini", "translate") == {}


def test_completion_kwargs_omits_reasoning_effort_for_openai_compatible_gemini(
    monkeypatch,
):
    monkeypatch.setattr(config, "GEMINI_OPENAI_COMPATIBLE", True)

    assert provider_resolver.completion_kwargs_for_task("gemini", "summary") == {}


def test_run_ai_task_uses_resolved_models_with_fallback_and_kwarg_override(monkeypatch):
    calls = []
    messages = [{"role": "user", "content": "hello"}]

    monkeypatch.setattr(
        task_runner,
        "get_provider_order_for_task",
        lambda task: ["gemini"],
    )
    monkeypatch.setattr(
        task_runner,
        "get_models_for_task",
        lambda provider, task: ["primary-model", "fallback-model"],
    )
    monkeypatch.setattr(
        task_runner,
        "_provider_completion_kwargs",
        lambda provider, task: {"reasoning_effort": "high", "temperature": 1},
    )

    def fake_create_chat_completion(provider, model, request_messages, **kwargs):
        calls.append(
            {
                "provider": provider,
                "model": model,
                "messages": request_messages,
                "kwargs": kwargs,
            }
        )
        if model == "primary-model":
            raise RuntimeError("primary failed")
        return "ok"

    monkeypatch.setattr(
        task_runner,
        "create_chat_completion",
        fake_create_chat_completion,
    )

    result = task_runner.run_ai_task("summary", messages, temperature=0)

    assert result == "ok"
    assert [call["model"] for call in calls] == ["primary-model", "fallback-model"]
    assert calls[0]["messages"] is messages
    assert calls[0]["kwargs"] == {
        "reasoning_effort": "high",
        "temperature": 0,
    }
