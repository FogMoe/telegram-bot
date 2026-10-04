import logging

from telegram.ext import ApplicationBuilder
from telegram.request import HTTPXRequest

from core import config
from core.telegram_history import HistoryTrackingExtBot, flush_all_pending_events
from features.conversation.lifecycle import post_init

from .handler_registry import register_handlers


async def _flush_telegram_history_on_stop(application) -> None:
    await flush_all_pending_events()


def _build_bot() -> HistoryTrackingExtBot:
    return HistoryTrackingExtBot(
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


def create_application(
    settings: config.AppSettings | None = None,
    *,
    bot: HistoryTrackingExtBot | None = None,
):
    """组装 Telegram Application。

    `settings` 是显式配置：传入时先让它成为生效配置（`config.install_settings`），之后 bot 和
    所有在调用时读 `config.<NAME>` 的代码都使用它；不传则使用进程启动时从 `.env` 加载的那份。
    `bot` 用于注入预先构建好的 bot（测试里的替身），不传则按配置构建。
    调用方负责在不再需要时恢复配置，一次性使用 `config.use_settings(settings)`。
    """
    if settings is not None:
        config.install_settings(settings)

    application = (
        ApplicationBuilder()
        .bot(bot if bot is not None else _build_bot())
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
