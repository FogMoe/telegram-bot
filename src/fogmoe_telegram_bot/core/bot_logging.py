import logging
import sys
from logging.handlers import RotatingFileHandler

from fogmoe_telegram_bot.core import config
from fogmoe_telegram_bot.core.redaction import RedactingFilter

LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


def build_handlers() -> list[logging.Handler]:
    """日志出口的唯一构造点：轮转文件始终启用，stdout 供 docker logs 等容器日志使用。"""
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        RotatingFileHandler(
            config.LOG_FILE_PATH,
            maxBytes=1 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
    ]

    if config.LOG_TO_STDOUT:
        # 非 UTF-8 终端（如 Windows 控制台）遇到无法编码的字符时不让日志输出抛错。
        reconfigure = getattr(sys.stdout, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="backslashreplace")
        handlers.append(logging.StreamHandler(sys.stdout))

    return handlers


def configure_logging() -> None:
    log_level = getattr(logging, config.LOG_LEVEL.upper(), logging.INFO)

    logging.basicConfig(
        level=log_level,
        format=LOG_FORMAT,
        handlers=build_handlers(),
    )

    # 所有 handler 统一挂脱敏 filter，日志文件与控制台都不会落地凭据。
    redacting_filter = RedactingFilter()
    for root_handler in logging.getLogger().handlers:
        root_handler.addFilter(redacting_filter)
