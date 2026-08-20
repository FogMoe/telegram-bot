"""把对话历史的业务动作注入 core，保持 core 不反向依赖 features。

`core.telegram_history` 只负责写库并发出信号：历史溢出、快照生成、私聊命令
进入。摘要生成、recap 失效与会话锁属于对话业务，实现放在这里。
"""

from contextlib import asynccontextmanager

from telegram import Update
from telegram.ext import TypeHandler

from core import mysql_connection
from core.telegram_history import (
    HistoryHooks,
    prepare_update_history,
    set_history_hooks,
)


async def handle_history_overflow(user_id: int) -> None:
    """历史溢出：先尝试立即生成摘要，失败时退回后台排队。"""

    from features.ai import summary

    summary_text = await summary.generate_summary_immediately(user_id)
    if summary_text:
        await mysql_connection.async_update_latest_history_state_summary(
            user_id,
            summary_text,
        )
    else:
        summary.schedule_summary_generation(user_id)


def handle_snapshot_created(user_id: int) -> None:
    """新快照落库：后台补一份摘要。"""

    from features.ai import summary

    summary.schedule_summary_generation(user_id)


@asynccontextmanager
async def private_command_guard(user_id: int):
    """先让执行中的 recap 失效，再等待它持有的会话锁。

    这样命令与 recap 只能按先后顺序写入历史，同时不会等到 recap 结束后才
    发现用户已返回。
    """

    from features.ai import idle_followup
    from features.ai.conversation_locks import get_conversation_lock

    await idle_followup.note_incoming_private_message(user_id)
    async with get_conversation_lock(user_id):
        yield


def install_history_hooks() -> None:
    set_history_hooks(
        HistoryHooks(
            on_history_overflow=handle_history_overflow,
            on_snapshot_created=handle_snapshot_created,
            private_command_guard=private_command_guard,
        )
    )


def setup_history_handlers(application) -> None:
    """装配历史回调，并在所有业务 handler 之前挂上历史记录入口。"""

    install_history_hooks()
    application.add_handler(TypeHandler(Update, prepare_update_history), group=-100)
