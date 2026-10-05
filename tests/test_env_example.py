import re

from fogmoe_telegram_bot.core import config


def test_env_example_documents_advisor_settings():
    env_example = (config.BASE_DIR / ".env.example").read_text(encoding="utf-8")
    expected_names = {
        "AI_ADVISOR_PROVIDER",
        "AI_ADVISOR_FALLBACK_PROVIDER",
        "OPENAI_ADVISOR_MODEL",
        "OPENROUTER_ADVISOR_MODEL",
        "FOGMOE_ADVISOR_MODEL",
        "SILICONFLOW_ADVISOR_MODEL",
        "GEMINI_ADVISOR_MODEL",
        "AZURE_OPENAI_ADVISOR_MODEL",
        "ZHIPU_ADVISOR_MODEL",
    }

    assert all(f"{name}=" in env_example for name in expected_names)


def test_env_example_documents_recap_settings():
    env_example = (config.BASE_DIR / ".env.example").read_text(encoding="utf-8")
    expected_names = {
        "AI_RECAP_PROVIDER",
        "AI_RECAP_FALLBACK_PROVIDER",
        "OPENAI_RECAP_MODEL",
        "OPENROUTER_RECAP_MODEL",
        "FOGMOE_RECAP_MODEL",
        "SILICONFLOW_RECAP_MODEL",
        "GEMINI_RECAP_MODEL",
        "AZURE_OPENAI_RECAP_MODEL",
        "ZHIPU_RECAP_MODEL",
    }

    assert all(f"{name}=" in env_example for name in expected_names)


RUNTIME_SETTINGS = (
    "CHAT_MAX_CONCURRENT_TURNS",
    "CHAT_MAX_QUEUED_TURNS",
    "CHAT_MAX_PENDING_PER_USER",
    "CHAT_QUEUE_MAX_WAIT_SECONDS",
    "CHAT_TURN_DEADLINE_SECONDS",
    "TELEGRAM_CONCURRENT_UPDATES",
    "BLOCKING_TOOL_THREADS",
    "BLOCKING_IO_THREADS",
    "RUNTIME_METRICS_LOG_INTERVAL_SECONDS",
    "RUNTIME_SHUTDOWN_GRACE_SECONDS",
)


def test_env_example_documents_every_runtime_setting_with_its_code_default():
    env_example = (config.BASE_DIR / ".env.example").read_text(encoding="utf-8")

    for name in RUNTIME_SETTINGS:
        match = re.search(rf"^# {name}=(\S+)$", env_example, re.MULTILINE)
        assert match, f"{name} is not documented in .env.example"
        default = config.AppSettings.model_fields[name].default
        assert float(match.group(1)) == float(default), f"{name} documents a different default"
