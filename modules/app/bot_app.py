import logging

from telegram.ext import Application, ApplicationBuilder
from telegram.request import HTTPXRequest

from core import config
from core.telegram_history import HistoryTrackingExtBot
from features.conversation.lifecycle import post_init

from . import runtime_lifecycle
from .handler_registry import register_handlers


class BotApplication(Application):  # type: ignore[type-arg]
    """在 PTB 开始停止的第一刻通知运行时：关闭准入、给在途轮次宽限。

    PTB 的 `stop()` 会等所有在途的 handler 结束才进入 `post_stop`；不提前通知的话，
    排队中的对话会白白等到自己的截止时间。顺序见 `runtime_lifecycle`。
    """

    __slots__ = ()

    async def stop(self) -> None:
        runtime_lifecycle.begin_shutdown()
        await super().stop()


async def _post_init(application) -> None:
    await post_init(application)
    runtime_lifecycle.start_runtime()


async def _post_stop(application) -> None:
    await runtime_lifecycle.shutdown_runtime()


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

    # concurrent_updates 取有界值而不是 True（256）：取值依据见 docs/runtime.md 的「准入」。
    application = (
        ApplicationBuilder()
        .application_class(BotApplication)
        .bot(bot if bot is not None else _build_bot())
        .concurrent_updates(config.TELEGRAM_CONCURRENT_UPDATES)
        .post_init(_post_init)
        .post_stop(_post_stop)
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
