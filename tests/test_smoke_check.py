from app import smoke_check
from core import config


def test_smoke_check_assembles_application_without_token(monkeypatch, capsys):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", None)

    assert smoke_check.run_smoke_check() == 0
    assert "smoke check ok" in capsys.readouterr().out


def test_smoke_check_fails_when_no_handlers_registered(monkeypatch, capsys):
    class EmptyApplication:
        handlers: dict = {}
        job_queue = None
        error_handlers: dict = {}

    monkeypatch.setattr(smoke_check, "create_application", lambda: EmptyApplication())

    assert smoke_check.run_smoke_check() == 1
    assert "no handlers" in capsys.readouterr().err
