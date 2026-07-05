import logging
from logging.handlers import RotatingFileHandler

from core import config


def configure_logging() -> None:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        config.LOG_FILE_PATH,
        maxBytes=1 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )

    log_level = getattr(logging, config.LOG_LEVEL.upper(), logging.INFO)

    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[handler],
    )
