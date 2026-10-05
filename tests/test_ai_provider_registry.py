"""provider 声明表：声明的配置键真实存在，名字与别名解析一致，声明驱动的各项行为。"""

import asyncio
from dataclasses import replace
from types import MappingProxyType, SimpleNamespace

import pytest

from fogmoe_telegram_bot.core import ai_providers, config
from fogmoe_telegram_bot.core.ai_providers import Capabilities, WireProtocol
from fogmoe_telegram_bot.features.ai import chat_capabilities, provider_resolver, task_runner
from fogmoe_telegram_bot.features.ai.litellm_message_sanitizer import sanitize_message_for_provider
from fogmoe_telegram_bot.features.ai.litellm_provider_config import provider_params

SETTING_NAMES = set(config.AppSettings.model_fields)
TASK_NAMES = tuple(ai_providers.TASK_SPECS)


def test_declared_config_keys_exist_in_settings():
    for spec in ai_providers.PROVIDERS:
        credentials = spec.credentials
        keys = [
            credentials.api_key,
            credentials.api_base,
            credentials.api_base_fallback,
            credentials.api_version,
            spec.openai_compatible_flag,
            *(spec.model_key(task) for task in TASK_NAMES),
            *spec.fallback_models.values(),
        ]
        missing = [key for key in keys if key is not None and key not in SETTING_NAMES]
        assert missing == [], f"{spec.name} declares unknown settings: {missing}"


def test_task_provider_keys_exist_in_settings():
    for task_spec in ai_providers.TASK_SPECS.values():
        if task_spec.provider_config_prefix is None:
            continue
        prefix = task_spec.provider_config_prefix
        assert f"{prefix}_PROVIDER" in SETTING_NAMES
        assert f"{prefix}_FALLBACK_PROVIDER" in SETTING_NAMES


def test_provider_names_and_aliases_are_unique_and_resolve_to_one_spec():
    seen: dict[str, str] = {}
    for spec in ai_providers.PROVIDERS:
        for name in (spec.name, *spec.aliases):
            assert name not in seen, f"{name} is claimed by {seen[name]} and {spec.name}"
            seen[name] = spec.name
            assert ai_providers.lookup(name) is spec


def test_lookup_ignores_case_and_whitespace_and_zhipu_is_zai():
    assert ai_providers.lookup(" OpenAI ") is ai_providers.lookup("openai")
    assert ai_providers.lookup("zhipu") is ai_providers.lookup("zai")
    assert ai_providers.require("ZHIPU").name == "zai"
    assert ai_providers.lookup("nope") is None
    assert ai_providers.lookup(None) is None


def test_require_rejects_unknown_provider():
    with pytest.raises(RuntimeError, match="Unsupported AI provider: nope"):
        ai_providers.require("nope")


@pytest.mark.parametrize(
    ("provider", "model", "compat", "expected"),
    [
        ("openai", "gpt-x", False, "openai/gpt-x"),
        ("openai", "azure/gpt-x", False, "azure/gpt-x"),
        ("openrouter", "openai/gpt-x", False, "openrouter/openai/gpt-x"),
        ("openrouter", "openrouter/vendor/m", False, "openrouter/vendor/m"),
        ("fogmoe", "openai/gpt-x", False, "openai/openai/gpt-x"),
        ("siliconflow", "deepseek-ai/m", False, "openai/deepseek-ai/m"),
        ("azure", "deployment", False, "azure/deployment"),
        ("gemini", "gemini-pro", False, "gemini/gemini-pro"),
        ("gemini", "gemini-pro", True, "openai/gemini-pro"),
        ("zai", "glm-x", False, "zai/glm-x"),
        ("zhipu", "glm-x", False, "zai/glm-x"),
    ],
)
def test_litellm_model_name_follows_each_providers_declared_rule(
    provider, model, compat, expected
):
    settings = SimpleNamespace(GEMINI_OPENAI_COMPATIBLE=compat)

    assert ai_providers.litellm_model_name(provider, model, settings) == expected


def test_litellm_model_name_requires_a_model():
    with pytest.raises(RuntimeError, match="Missing model configuration for provider: openai"):
        ai_providers.litellm_model_name("openai", "")


def test_gemini_wire_protocol_depends_on_the_openai_compatible_flag():
    gemini = ai_providers.require("gemini")

    native = gemini.wire_protocol_for(SimpleNamespace(GEMINI_OPENAI_COMPATIBLE=False))
    compatible = gemini.wire_protocol_for(SimpleNamespace(GEMINI_OPENAI_COMPATIBLE=True))

    assert native.keeps_provider_specific_fields and native.strips_tool_call_ids
    assert compatible == ai_providers.OPENAI_WIRE
    assert all(
        spec.wire_protocol_for() == ai_providers.OPENAI_WIRE
        for spec in ai_providers.PROVIDERS
        if spec.name != "gemini"
    )


def test_sanitizer_follows_an_explicit_protocol_over_the_provider_name():
    message = {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": "x",
        "provider_specific_fields": {"a": 1},
    }

    kept = sanitize_message_for_provider(message, "openai", protocol=WireProtocol(
        keeps_provider_specific_fields=True,
    ))
    stripped = sanitize_message_for_provider(message, "gemini", protocol=ai_providers.OPENAI_WIRE)

    assert kept["provider_specific_fields"] == {"a": 1} and kept["tool_call_id"] == "call-1"
    assert "provider_specific_fields" not in stripped and stripped["tool_call_id"] == "call-1"


def test_configured_models_lists_primary_then_fallback_without_deduplicating():
    settings = SimpleNamespace(GEMINI_CHAT_MODEL="a", GEMINI_CHAT_FALLBACK_MODEL="a")

    assert ai_providers.configured_models("gemini", "chat", settings) == ["a", "a"]
    assert ai_providers.configured_models("gemini", "vision", settings) == [None]
    assert ai_providers.configured_models("unknown", "chat", settings) == []


def test_provider_resolution_reads_the_injected_settings_not_global_config():
    settings = SimpleNamespace(
        AI_SERVICE_ORDER=["siliconflow", "openai"],
        AI_SUMMARY_PROVIDER="Zhipu",
        AI_SUMMARY_FALLBACK_PROVIDER="openai",
        ZHIPU_SUMMARY_MODEL="glm-summary",
        OPENAI_SUMMARY_MODEL="gpt-summary",
    )

    assert provider_resolver.get_provider_order_for_task("chat", settings) == [
        "siliconflow",
        "openai",
    ]
    assert provider_resolver.get_provider_order_for_task("summary", settings) == [
        "zhipu",
        "openai",
    ]
    assert provider_resolver.get_models_for_task("zhipu", "summary", settings) == [
        "glm-summary"
    ]


def test_provider_params_read_the_injected_settings():
    settings = SimpleNamespace(
        OPENROUTER_API_KEY="key",
        OPENROUTER_API_BASE="https://router.test/v1/chat/completions",
    )

    assert provider_params("openrouter", settings) == {
        "api_key": "key",
        "api_base": "https://router.test/v1",
    }
    with pytest.raises(RuntimeError, match="Missing OPENROUTER_API_KEY"):
        provider_params("openrouter", SimpleNamespace(OPENROUTER_API_BASE="https://x"))


def test_settings_override_is_seen_by_provider_resolution(settings_override):
    settings_override(
        AI_CHAT_ORDER="openai, zhipu",
        AI_VISION_PROVIDER="gemini",
        GEMINI_VISION_MODEL="gemini-vision",
    )

    assert provider_resolver.get_provider_order_for_task("chat") == ["openai", "zhipu"]
    assert provider_resolver.get_models_for_task("gemini", "vision") == ["gemini-vision"]
    assert provider_resolver.get_provider_order_for_task("vision") == ["gemini"]


def _register_without_vision(monkeypatch, provider: str) -> None:
    patched = {
        key: (
            replace(spec, capabilities=Capabilities(tools=True, vision=False))
            if spec.name == provider
            else spec
        )
        for key, spec in ai_providers._BY_NAME.items()
    }
    monkeypatch.setattr(ai_providers, "_BY_NAME", MappingProxyType(patched))


def test_a_provider_without_vision_is_skipped_for_the_vision_task(monkeypatch):
    _register_without_vision(monkeypatch, "openai")
    calls = []
    monkeypatch.setattr(
        task_runner,
        "get_provider_order_for_task",
        lambda task: ["openai", "gemini"],
    )
    monkeypatch.setattr(task_runner, "get_models_for_task", lambda p, t: [f"{p}-model"])

    async def fake_create(provider, model, messages, **kwargs):
        calls.append(provider)
        return "ok"

    monkeypatch.setattr(task_runner, "create_chat_completion", fake_create)

    assert asyncio.run(task_runner.run_ai_task("vision", [{"role": "user", "content": "x"}])) == "ok"
    assert calls == ["gemini"]


def test_a_provider_without_vision_never_receives_images_in_chat(monkeypatch):
    _register_without_vision(monkeypatch, "openai")
    settings = SimpleNamespace(OPENAI_CHAT_MODEL="gpt", AI_CHAT_TEXT_ONLY_MODELS=[])

    assert chat_capabilities.chat_service_supports_vision("openai", settings) is False
    assert chat_capabilities.chat_service_supports_vision("gemini", settings) is True
