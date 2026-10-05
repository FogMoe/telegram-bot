"""金币转账（/give）与富豪榜（/rich）的业务操作。不依赖 Telegram。

赠送：发送者扣款（本金 + 手续费）、收款人入账、每日次数在同一个事务里，op_key 见
docs/balance-service.md。
"""

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_telegram_bot.core import balance, user_records
from fogmoe_telegram_bot.core.command_identity import message_identity

from ..repositories import coins as coins_repository
from ..repositories.coins import RichEntry

GIVE_DAILY_LIMIT = 5


def calculate_give_fee(amount: int) -> int:
    if amount <= 1:
        return 0
    fee = amount // 5
    return fee if fee >= 1 else 1


class GiveStatus(StrEnum):
    GIVEN = "given"
    REPLAYED = "replayed"  # 同一条命令被重复投递，上一次已经完整执行
    CONFLICT = "conflict"  # 这个身份已经用于另一笔赠送（收款人或金额不同），本次什么都没做
    NOT_REGISTERED = "not_registered"
    INSUFFICIENT = "insufficient"
    DAILY_LIMIT = "daily_limit"
    RECIPIENT_NOT_FOUND = "recipient_not_found"
    SELF = "self"


@dataclass(frozen=True)
class GiveOutcome:
    status: GiveStatus
    balance_total: int = 0  # 余额不足时发送者当前的余额


def give_op_key(chat_id: int, message_id: int) -> str:
    """赠送的身份：命令消息（AI 代执行时再带上这次代执行的身份，见 core/command_identity.py）。

    发送者的扣款（含手续费）用它，收款人的入账再加 `:recv`。
    """
    return balance.make_op_key("give", *message_identity(chat_id, message_id))


def give_recipient_op_key(chat_id: int, message_id: int) -> str:
    return balance.make_op_key("give", *message_identity(chat_id, message_id), "recv")


async def find_recipient_id(target_name: str) -> int | None:
    """按用户名解析收款人 id；要在事务之前做，转账事务需要先按 id 升序锁住双方。"""
    return await user_records.find_id_by_name(target_name)


async def transfer_coins(
    sender_id: int,
    recipient_id: int | None,
    amount: int,
    *,
    sender_op_key: str,
    recipient_op_key: str,
    today: date,
) -> GiveOutcome:
    """赠送：发送者扣款（本金 + 手续费）、收款人入账、每日次数在同一个事务里。

    收款人 id 在事务之前按名字解析出来，这样才能在事务开头按 user id 升序同时锁住双方
    （A 赠 B 与 B 赠 A 同时发生也不会互相等待）。每日次数在锁内读取与累加，所以并发的
    多次赠送不会突破上限。检查顺序与旧实现一致：未注册、余额不足、次数上限、收款人不存在、自赠。
    """
    fee = calculate_give_fee(amount)
    total_cost = amount + fee

    async def work(connection: AsyncConnection) -> GiveOutcome:
        to_lock = [sender_id]
        if recipient_id is not None:
            to_lock.append(recipient_id)
        try:
            locked = await balance.lock_users(connection, to_lock)
        except balance.UserNotFound as exc:
            if exc.user_id == sender_id:
                return GiveOutcome(GiveStatus.NOT_REGISTERED)
            return GiveOutcome(GiveStatus.RECIPIENT_NOT_FOUND)

        # 重放要最先判断：它已经计入了当天的次数，也已经花掉了余额。参数不同的不是重放，
        # 而是身份被另一笔赠送占用了，不能当作成功。
        existing = await balance.get_operation(sender_op_key, connection=connection)
        if existing is not None:
            if _is_same_give(existing, sender_id, recipient_id, total_cost):
                return GiveOutcome(GiveStatus.REPLAYED)
            return GiveOutcome(GiveStatus.CONFLICT)

        sender_total = locked[sender_id].total
        if sender_total < total_cost:
            return GiveOutcome(GiveStatus.INSUFFICIENT, sender_total)

        # 发送者的行已锁住，这一行只会被他自己的赠送事务读写；普通读即可，
        # 对可能不存在的键做 FOR UPDATE 会让不同用户的首次写入互相死锁。
        given_today = await coins_repository.get_daily_give_count(connection, sender_id, today)
        if given_today >= GIVE_DAILY_LIMIT:
            return GiveOutcome(GiveStatus.DAILY_LIMIT)
        if recipient_id is None:
            return GiveOutcome(GiveStatus.RECIPIENT_NOT_FOUND)
        if recipient_id == sender_id:
            return GiveOutcome(GiveStatus.SELF)

        try:
            await balance.debit(
                connection,
                sender_id,
                total_cost,
                op_key=sender_op_key,
                reason="give",
                ref=f"to:{recipient_id}",
            )
        except balance.InsufficientBalance as exc:
            return GiveOutcome(GiveStatus.INSUFFICIENT, exc.balance_total)
        await balance.credit(
            connection,
            recipient_id,
            amount,
            op_key=recipient_op_key,
            reason="give_received",
            ref=f"from:{sender_id}",
        )
        await coins_repository.increment_daily_give_count(connection, sender_id, today)
        return GiveOutcome(GiveStatus.GIVEN)

    return await balance.run_in_transaction(work)


def _is_same_give(
    existing: balance.BalanceResult,
    sender_id: int,
    recipient_id: int | None,
    total_cost: int,
) -> bool:
    return (
        existing.kind is balance.LedgerKind.DEBIT
        and existing.user_id == sender_id
        and -(existing.delta_free + existing.delta_paid) == total_cost
        and existing.ref == f"to:{recipient_id}"
    )


async def richest_users(limit: int = 5) -> list[RichEntry]:
    """金币（免费加付费）最多的前 `limit` 个用户。"""
    return await coins_repository.richest_users(limit)
