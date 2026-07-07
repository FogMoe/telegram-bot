from core import config
from features.ai import chat_capabilities


def test_chat_model_supports_vision_respects_text_only_patterns(monkeypatch):
    monkeypatch.setattr(
        config,
        "AI_CHAT_TEXT_ONLY_MODELS",
        ["deepseek-ai/deepseek-v4-flash", "vendor/text-*"],
    )

    assert (
        chat_capabilities.chat_model_supports_vision(
            "deepseek-ai/DeepSeek-V4-Flash",
        )
        is False
    )
    assert chat_capabilities.chat_model_supports_vision("vendor/text-small") is False
    assert chat_capabilities.chat_model_supports_vision("gpt-4o") is True


def test_chat_model_for_service_uses_chat_task_model_config(monkeypatch):
    monkeypatch.setattr(config, "SILICONFLOW_CHAT_MODEL", "deepseek-ai/model")

    assert chat_capabilities.chat_model_for_service("siliconflow") == "deepseek-ai/model"
