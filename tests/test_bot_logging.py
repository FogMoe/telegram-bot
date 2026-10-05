import logging
import sys
from logging.handlers import RotatingFileHandler

from fogmoe_telegram_bot.core import bot_logging, config


def _use_log_dir(monkeypatch, tmp_path):
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(config, "LOG_DIR", log_dir)
    monkeypatch.setattr(config, "LOG_FILE_PATH", log_dir / "tgbot.log")
    return log_dir


def _close(handlers):
    for handler in handlers:
        handler.close()


def test_handlers_write_rotating_file_and_stdout(monkeypatch, tmp_path):
    log_dir = _use_log_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "LOG_TO_STDOUT", True)

    handlers = bot_logging.build_handlers()
    try:
        file_handlers = [h for h in handlers if isinstance(h, RotatingFileHandler)]
        stream_handlers = [
            h
            for h in handlers
            if isinstance(h, logging.StreamHandler)
            and not isinstance(h, RotatingFileHandler)
        ]
        assert [h.baseFilename for h in file_handlers] == [str(log_dir / "tgbot.log")]
        assert [h.stream for h in stream_handlers] == [sys.stdout]
    finally:
        _close(handlers)


def test_stdout_handler_can_be_disabled(monkeypatch, tmp_path):
    _use_log_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "LOG_TO_STDOUT", False)

    handlers = bot_logging.build_handlers()
    try:
        assert len(handlers) == 1
        assert isinstance(handlers[0], RotatingFileHandler)
    finally:
        _close(handlers)
