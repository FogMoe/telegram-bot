from core import config


def test_text_resources_are_loaded_verbatim():
    assert config.HELP_TEXT == (
        config.BASE_DIR / "resources" / "telegram_help.md"
    ).read_text(encoding="utf-8")
    assert config.SYSTEM_PROMPT == (
        config.BASE_DIR / "resources" / "prompts" / "system_prompt.md"
    ).read_text(encoding="utf-8")
    assert config.ADVISOR_SYSTEM_PROMPT == (
        config.BASE_DIR / "resources" / "prompts" / "advisor_system_prompt.md"
    ).read_text(encoding="utf-8")


def test_system_prompt_resource_preserves_markdown_line_breaks():
    assert config.SYSTEM_PROMPT.startswith(
        "# Character Profile of FOGMOE\n## Core Identity\n- "
    )
    assert "\n# Tool Calling\n## Calling Rules\n- " in config.SYSTEM_PROMPT
    assert "\n# Runtime Context\n## Message Format\n" in config.SYSTEM_PROMPT


def test_system_prompt_defines_telegram_meta_agent_events():
    prompt = config.SYSTEM_PROMPT

    assert 'event="command"' in prompt
    assert 'type="user_event"' in prompt
    assert 'type="bot_event"' in prompt
    assert "<displayed_message>" in prompt
    assert "without metadata" in prompt
    assert config.SYSTEM_PROMPT.endswith("\n")


def test_env_example_documents_advisor_settings():
    env_example = (config.BASE_DIR / ".env.example").read_text(encoding="utf-8")
    expected_names = {
        "AI_ADVISOR_PROVIDER",
        "AI_ADVISOR_FALLBACK_PROVIDER",
        "OPENAI_ADVISOR_MODEL",
        "SILICONFLOW_ADVISOR_MODEL",
        "GEMINI_ADVISOR_MODEL",
        "AZURE_OPENAI_ADVISOR_MODEL",
        "ZHIPU_ADVISOR_MODEL",
    }

    assert all(f"{name}=" in env_example for name in expected_names)
