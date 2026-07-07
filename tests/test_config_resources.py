from core import config


def test_text_resources_are_loaded_verbatim():
    assert config.HELP_TEXT == (
        config.BASE_DIR / "resources" / "telegram_help.md"
    ).read_text(encoding="utf-8")
    assert config.SYSTEM_PROMPT == (
        config.BASE_DIR / "resources" / "prompts" / "system_prompt.md"
    ).read_text(encoding="utf-8")


def test_system_prompt_resource_preserves_markdown_line_breaks():
    assert config.SYSTEM_PROMPT.startswith(
        "# Character Profile of FogMoeBot\n## Core Identity\n- "
    )
    assert "\n# Tool Calling\n## Calling Rules\n- " in config.SYSTEM_PROMPT
    assert config.SYSTEM_PROMPT.endswith("by themselves\n")
