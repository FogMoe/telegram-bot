import logging

from telegram.ext import ApplicationBuilder

from core import config
from core.bot_conversation import post_init

from .handler_registry import register_handlers


def create_application():
    application = (
        ApplicationBuilder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .connect_timeout(config.TELEGRAM_CONNECT_TIMEOUT)
        .read_timeout(config.TELEGRAM_READ_TIMEOUT)
        .write_timeout(config.TELEGRAM_WRITE_TIMEOUT)
        .pool_timeout(config.TELEGRAM_POOL_TIMEOUT)
        .get_updates_connect_timeout(config.TELEGRAM_GET_UPDATES_CONNECT_TIMEOUT)
        .get_updates_read_timeout(config.TELEGRAM_GET_UPDATES_READ_TIMEOUT)
        .get_updates_write_timeout(config.TELEGRAM_GET_UPDATES_WRITE_TIMEOUT)
        .get_updates_pool_timeout(config.TELEGRAM_GET_UPDATES_POOL_TIMEOUT)
        .concurrent_updates(True)
        .post_init(post_init)
        .build()
    )

    register_handlers(application)
    return application


def run() -> None:
    application = create_application()
    try:
        application.run_polling(timeout=config.TELEGRAM_GET_UPDATES_TIMEOUT)
    except KeyboardInterrupt:
        logging.info("Bot shutdown requested by keyboard interrupt.")
