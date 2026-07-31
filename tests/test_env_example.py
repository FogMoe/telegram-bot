from core import config


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
