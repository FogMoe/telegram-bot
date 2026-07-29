from core.bot_conversation import post_init as initialize_conversation
from features.account.fogmoe_account import (
    start_callback_server,
    stop_callback_server,
)


async def post_init(application) -> None:
    await initialize_conversation(application)
    await start_callback_server(application)


async def post_shutdown(application) -> None:
    await stop_callback_server(application)
