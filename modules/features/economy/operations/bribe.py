"""/bribe 的业务操作：花金币提升好感度。不依赖 Telegram。

扣款与好感度变化在同一个事务里：好感度写入失败时金币一并回滚。每 100 金币随机提升 1-10 点好感度，
好感度到 100 后不再继续提升。
"""

import random
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncConnection

from core import balance, process_user
from core.command_identity import message_identity


class BribeStatus(StrEnum):
    PAID = "paid"
    REPLAYED = "replayed"
    INSUFFICIENT = "insufficient"
    NOT_REGISTERED = "not_registered"


@dataclass(frozen=True)
class BribeOutcome:
    status: BribeStatus
    balance_total: int = 0
    total_gain: int = 0
    affection_after: int = 0


def bribe_op_key(chat_id: int, message_id: int) -> str:
    """贿赂扣款：以命令消息为身份，同一条 /bribe 被重复投递不会再扣一次。"""
    return balance.make_op_key("bribe", *message_identity(chat_id, message_id))


async def pay_bribe(
    user_id: int,
    coins_to_spend: int,
    affection_before: int,
    op_key: str,
) -> BribeOutcome:
    """扣款与好感度变化在同一个事务里：好感度写入失败时金币一并回滚。"""

    async def work(connection: AsyncConnection) -> BribeOutcome:
        try:
            balances = await balance.lock_user(connection, user_id)
        except balance.UserNotFound:
            return BribeOutcome(BribeStatus.NOT_REGISTERED)
        if await balance.get_operation(op_key, connection=connection) is not None:
            return BribeOutcome(BribeStatus.REPLAYED)
        if balances.total < coins_to_spend:
            return BribeOutcome(BribeStatus.INSUFFICIENT, balance_total=balances.total)
        try:
            await balance.debit(
                connection, user_id, coins_to_spend, op_key=op_key, reason="bribe"
            )
        except balance.InsufficientBalance as exc:
            return BribeOutcome(BribeStatus.INSUFFICIENT, balance_total=exc.balance_total)

        current_affection = affection_before
        total_gain = 0
        for _ in range(coins_to_spend // 100):
            delta = random.randint(1, 10)
            new_affection = await process_user.update_user_affection(
                user_id, delta, connection=connection
            )
            total_gain += max(0, new_affection - current_affection)
            current_affection = new_affection
            if current_affection >= 100:
                break
        return BribeOutcome(
            BribeStatus.PAID, total_gain=total_gain, affection_after=current_affection
        )

    return await balance.run_in_transaction(work)
