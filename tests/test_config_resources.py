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
    assert config.IDLE_RECAP_SYSTEM_PROMPT == (
        config.BASE_DIR / "resources" / "prompts" / "idle_recap_system_prompt.md"
    ).read_text(encoding="utf-8")
    assert "memory_suggestion.impression" in config.IDLE_RECAP_SYSTEM_PROMPT
    assert "memory_suggestion.diary" in config.IDLE_RECAP_SYSTEM_PROMPT
    assert "fetch_permanent_summaries" in config.IDLE_RECAP_SYSTEM_PROMPT
    assert "search_permanent_records" in config.IDLE_RECAP_SYSTEM_PROMPT
    assert "`recap` 是已发生对话内容的简短概括" in config.IDLE_RECAP_SYSTEM_PROMPT
    assert "`open_loops` 是尚未完成或值得稍后确认的事项，没有则返回空字符串" in (
        config.IDLE_RECAP_SYSTEM_PROMPT
    )
    assert "`suggested_follow_up` 是适合自然跟进的方向，没有则返回空字符串" in (
        config.IDLE_RECAP_SYSTEM_PROMPT
    )


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
    assert 'origin="ai_tool" delegated="true"' in prompt
    assert "### execute_telegram_command" in prompt
    assert "### get_help_text" in prompt
    assert "On a message-shaped turn" in prompt
    assert "FOGMOE's own lookup" in prompt
    assert "reference context, not a command executed by the user" in prompt
    assert 'origin="idle_recap"' in prompt
    assert "written by a separate recap agent" in prompt
    assert "<memory_suggestion>" in prompt
    assert "update_impression" in prompt
    assert "user_diary" in prompt
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


def test_env_example_documents_recap_settings():
    env_example = (config.BASE_DIR / ".env.example").read_text(encoding="utf-8")
    expected_names = {
        "AI_RECAP_PROVIDER",
        "AI_RECAP_FALLBACK_PROVIDER",
        "OPENAI_RECAP_MODEL",
        "SILICONFLOW_RECAP_MODEL",
        "GEMINI_RECAP_MODEL",
        "AZURE_OPENAI_RECAP_MODEL",
        "ZHIPU_RECAP_MODEL",
    }

    assert all(f"{name}=" in env_example for name in expected_names)
