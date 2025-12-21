import logging
from logging.handlers import RotatingFileHandler

import config


def configure_logging() -> None:
    handler = RotatingFileHandler(
        config.BASE_DIR / "tgbot.log",
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
