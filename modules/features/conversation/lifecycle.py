"""对话功能的启动装配与 bot 身份缓存。

身份在 post_init 时拉取一次；群聊路径在缺失时会再补一次，因此这里保留可写的
模块级缓存，读取方通过 `lifecycle.<name>` 访问以拿到最新值。
"""

import asyncio
import logging

import telegram

from core import db, group_chat_history

logger = logging.getLogger(__name__)


_BOT_ID: int | None = None


_BOT_USERNAME: str = "FogMoeBot"


def _cache_bot_identity(bot_user: telegram.User) -> None:
    """Cache bot identity globally and notify group history module."""
    global _BOT_ID, _BOT_USERNAME
    _BOT_ID = bot_user.id
    _BOT_USERNAME = bot_user.username or "FogMoeBot"
    group_chat_history.set_bot_identity(_BOT_ID, _BOT_USERNAME)


async def _refresh_bot_identity(bot, *, source: str) -> bool:
    try:
        bot_user = await bot.get_me()
    except telegram.error.NetworkError as exc:
        logger.warning(
            "Unable to fetch bot identity during %s; will retry later: %r",
            source,
            exc,
        )
        return False
    _cache_bot_identity(bot_user)
    return True


async def post_init(application) -> None:
    main_loop = asyncio.get_running_loop()
    db.set_main_loop(main_loop)
    from features.ai.telegram_command_executor import (
        configure_telegram_command_executor,
    )

    configure_telegram_command_executor(application, main_loop)
    await _refresh_bot_identity(application.bot, source="post_init")
