import logging

from telegram.ext import ApplicationBuilder
from telegram.request import HTTPXRequest

from core import config
from features.conversation.lifecycle import post_init
from core.telegram_history import HistoryTrackingExtBot, flush_all_pending_events

from .handler_registry import register_handlers


async def _flush_telegram_history_on_stop(application) -> None:
    await flush_all_pending_events()


def create_application():
    bot = HistoryTrackingExtBot(
        token=config.TELEGRAM_BOT_TOKEN,
        request=HTTPXRequest(
            connect_timeout=config.TELEGRAM_CONNECT_TIMEOUT,
            read_timeout=config.TELEGRAM_READ_TIMEOUT,
            write_timeout=config.TELEGRAM_WRITE_TIMEOUT,
            pool_timeout=config.TELEGRAM_POOL_TIMEOUT,
        ),
        get_updates_request=HTTPXRequest(
            connection_pool_size=1,
            connect_timeout=config.TELEGRAM_GET_UPDATES_CONNECT_TIMEOUT,
            read_timeout=config.TELEGRAM_GET_UPDATES_READ_TIMEOUT,
            write_timeout=config.TELEGRAM_GET_UPDATES_WRITE_TIMEOUT,
            pool_timeout=config.TELEGRAM_GET_UPDATES_POOL_TIMEOUT,
        ),
    )
    application = (
        ApplicationBuilder()
        .bot(bot)
        .concurrent_updates(True)
        .post_init(post_init)
        .post_stop(_flush_telegram_history_on_stop)
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
