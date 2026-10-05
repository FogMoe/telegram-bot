import pytest

from app import smoke_check
from core import config

pytestmark = pytest.mark.slow


def test_smoke_check_assembles_application_without_token(monkeypatch, capsys):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", None)

    assert smoke_check.run_smoke_check() == 0
    assert "smoke check ok" in capsys.readouterr().out


def test_smoke_check_does_not_leave_the_placeholder_token_behind(settings_override, capsys):
    settings_override(TELEGRAM_BOT_TOKEN=None)

    assert smoke_check.run_smoke_check() == 0

    assert config.TELEGRAM_BOT_TOKEN is None
    assert config.current_settings().TELEGRAM_BOT_TOKEN is None


def test_smoke_check_hands_the_assembly_an_explicit_settings_object(
    settings_override, monkeypatch, capsys
):
    seen = []

    class EmptyApplication:
        handlers: dict = {}
        job_queue = None
        error_handlers: dict = {}

    def fake_create_application(settings=None):
        seen.append((settings, config.TELEGRAM_BOT_TOKEN))
        return EmptyApplication()

    settings_override(TELEGRAM_BOT_TOKEN=None)
    monkeypatch.setattr(smoke_check, "create_application", fake_create_application)

    smoke_check.run_smoke_check()

    (settings, token_in_effect), = seen
    assert settings.TELEGRAM_BOT_TOKEN
    assert token_in_effect == settings.TELEGRAM_BOT_TOKEN


def test_smoke_check_fails_when_no_handlers_registered(monkeypatch, capsys):
    class EmptyApplication:
        handlers: dict = {}
        job_queue = None
        error_handlers: dict = {}

    monkeypatch.setattr(
        smoke_check, "create_application", lambda settings=None: EmptyApplication()
    )

    assert smoke_check.run_smoke_check() == 1
    assert "no handlers" in capsys.readouterr().err
