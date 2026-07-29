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


def test_help_keeps_webpassword_command_hidden():
    assert "/webpassword" not in config.HELP_TEXT


def test_system_prompt_resource_preserves_markdown_line_breaks():
    assert config.SYSTEM_PROMPT.startswith(
        "# Character Profile of FOGMOE\n## Core Identity\n- "
    )
    assert "\n# Tool Calling\n## Calling Rules\n- " in config.SYSTEM_PROMPT
    assert "\n# Runtime Context\n## Message Format\n" in config.SYSTEM_PROMPT
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


def test_env_example_documents_fogmoe_oauth_settings_without_a_secret():
    env_example = (config.BASE_DIR / ".env.example").read_text(encoding="utf-8")
    expected_names = {
        "FOGMOE_OAUTH_ENABLED",
        "FOGMOE_OAUTH_DISCOVERY_URL",
        "FOGMOE_OAUTH_EXPECTED_ISSUER",
        "FOGMOE_OAUTH_CLIENT_ID",
        "FOGMOE_OAUTH_CLIENT_SECRET",
        "FOGMOE_OAUTH_AUDIENCE",
        "FOGMOE_OAUTH_REDIRECT_URI",
        "FOGMOE_OAUTH_SCOPES",
        "FOGMOE_OAUTH_ALLOWED_ALGORITHMS",
        "FOGMOE_OAUTH_LISTEN_HOST",
        "FOGMOE_OAUTH_LISTEN_PORT",
    }

    assert all(f"{name}=" in env_example for name in expected_names)
    assert "FOGMOE_OAUTH_CLIENT_SECRET=\n" in env_example
    assert "FOGMOE_OAUTH_LISTEN_PORT=18765\n" in env_example


def test_fogmoe_oauth_port_is_only_published_by_the_opt_in_override():
    base_compose = (config.BASE_DIR / "docker-compose.yml").read_text(
        encoding="utf-8"
    )
    oauth_compose = (
        config.BASE_DIR / "docker-compose.fogmoe-oauth.yml"
    ).read_text(encoding="utf-8")

    assert "ports:" not in base_compose
    assert "FOGMOE_OAUTH_LISTEN_HOST" not in base_compose
    assert "127.0.0.1:${FOGMOE_OAUTH_LISTEN_PORT:-18765}" in oauth_compose
