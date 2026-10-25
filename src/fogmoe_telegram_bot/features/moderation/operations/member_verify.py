"""入群验证的业务规则：群的开关、每一轮验证的记录与认领、到期与恢复。不依赖 Telegram。

规则：

- 开启验证的群里，新成员入群后被禁言，`VERIFY_SECONDS` 秒内点验证按钮解除；到期仍被禁言的移出群组（不封禁）。
- 每个 (用户, 群组) 同时只有一轮验证，以欢迎消息为身份：重新入群换成新消息，旧消息上的按钮和旧定时器随之失效。
- 认领一轮验证就是删掉它的记录，删到的一方才继续处理，所以按钮点击、超时和离群对同一轮只有一个生效。
- 到点处理靠进程内的定时器，重启会丢掉它们；恢复任务按记录里的到期时间兜底。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fogmoe_telegram_bot.core import sql

from ..repositories import verification
from ..repositories.verification import VerificationTask

VERIFY_SECONDS = 300
UNKNOWN_GROUP_NAME = "未知群组"


async def is_enabled(chat_id: int) -> bool:
    return await verification.is_enabled(chat_id)


async def enable(chat_id: int, title: str | None) -> None:
    async with sql.transaction() as connection:
        await verification.enable(connection, chat_id, title or UNKNOWN_GROUP_NAME)


async def disable(chat_id: int) -> None:
    async with sql.transaction() as connection:
        await verification.disable(connection, chat_id)


async def forget_group(chat_id: int) -> None:
    """机器人被移出群：关掉验证，丢掉所有未完成的验证。"""
    async with sql.transaction() as connection:
        await verification.disable(connection, chat_id)
        await verification.delete_group_tasks(connection, chat_id)


async def open_round(user_id: int, chat_id: int, message_id: int) -> None:
    """以这条欢迎消息开始一轮验证（或在解除禁言失败后重新打开），从现在起 `VERIFY_SECONDS` 秒后到期。"""
    expire_time = datetime.now() + timedelta(seconds=VERIFY_SECONDS)
    async with sql.transaction() as connection:
        await verification.save_task(connection, user_id, chat_id, message_id, expire_time)


async def claim_round(user_id: int, chat_id: int, message_id: int | None) -> bool:
    """认领这一轮验证；已经被认领、或已经换成别的欢迎消息时返回 False。"""
    async with sql.transaction() as connection:
        return await verification.delete_task(connection, user_id, chat_id, message_id)


async def find_round_message(user_id: int, chat_id: int) -> int | None:
    return await verification.find_task_message(user_id, chat_id)


async def due_rounds() -> list[VerificationTask]:
    """已经到期、还没有被认领的验证。"""
    return await verification.list_due_tasks(datetime.now())
