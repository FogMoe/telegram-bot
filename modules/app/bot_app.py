import logging

from telegram.ext import ApplicationBuilder

from core import config
from core.bot_conversation import post_init

from .handler_registry import register_handlers


def create_application():
    application = (
        ApplicationBuilder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .concurrent_updates(True)
        .post_init(post_init)
        .build()
    )

    register_handlers(application)
    return application


def run() -> None:
    application = create_application()
    try:
        application.run_polling()
    except KeyboardInterrupt:
        logging.info("Bot shutdown requested by keyboard interrupt.")
