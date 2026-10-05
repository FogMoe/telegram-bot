"""AI 对话的扣费。

一轮对话可能包含批处理窗口里的多条消息。每条消息各自按持久身份记一笔账，整轮在同一个事务里
完成：任何一条余额不足整轮都不扣；奖池贡献与扣费同事务、带 op_key。

op_key 规则（`message_op_key`）：

- 普通消息：`chat:<chat_id>:<message_id>`
- 编辑后的消息：`chat:<chat_id>:<message_id>:edit:<edit_date 的 unix 秒>`，每次编辑单独计费，
  与原来「编辑也算一条新消息」的行为一致
- 拿不到 message_id（理论上不会出现）：`chat:<chat_id>:update:<update_id>`
- 连 update_id 也没有：一次性随机 key，没有重放保护

价格（`text_message_cost`、`MEDIA_COST`）：图片和贴纸固定 5 个币；文字按长度分档，
不超过 100 字符 1 个币，101-500 为 2，501-1000 为 3，1001-2000 为 4，2001-4096 为 5；
超过 `MAX_TEXT_LENGTH`（4096）的消息不处理、不扣费。

同一个 Telegram update 被重复投递时身份相同，`debit` 返回 `applied=False`：这条消息已经收过
钱，不再扣费、不再贡献奖池，这一轮仍然继续往下走（重复投递几乎只发生在进程中途被杀之后，
这时用户付了钱却没有得到回复，继续处理才是对的）。不同的消息、不同轮次的 op_key 各不相同，
照常扣费。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_telegram_bot.core import balance, mysql_connection, stake_reward_pool

CHAT_REASON = "ai_chat"

MEDIA_COST = 5
MAX_TEXT_LENGTH = 4096
# (长度下限（不含）, 价格)，从高到低；不超过最低一档的文字消息 1 个币。
_TEXT_COST_TIERS = ((2000, 5), (1000, 4), (500, 3), (100, 2))


def text_message_cost(length: int) -> int:
    """文字消息按长度阶梯计费。调用方应先用 `MAX_TEXT_LENGTH` 拒绝超长消息。"""
    for threshold, cost in _TEXT_COST_TIERS:
        if length > threshold:
            return cost
    return 1


@dataclass(frozen=True, slots=True)
class TurnMessage:
    """一轮对话里需要计费的一条消息。"""

    chat_id: int
    message_id: int | None
    cost: int
    edit_stamp: int | None = None  # 编辑后的消息：edit_date 的 unix 秒
    update_id: int | None = None

    @classmethod
    def from_message(
        cls,
        message: Any,
        *,
        chat_id: int,
        cost: int,
        edited: bool,
        update_id: int | None = None,
    ) -> TurnMessage:
        edit_stamp: int | None = None
        if edited:
            edit_date = getattr(message, "edit_date", None)
            edit_stamp = int(edit_date.timestamp()) if edit_date else (update_id or 0)
        return cls(
            chat_id=chat_id,
            message_id=getattr(message, "message_id", None),
            cost=cost,
            edit_stamp=edit_stamp,
            update_id=update_id,
        )


def message_op_key(message: TurnMessage) -> str:
    if message.message_id is not None:
        key = f"chat:{message.chat_id}:{message.message_id}"
        if message.edit_stamp is not None:
            key += f":edit:{message.edit_stamp}"
        return balance.make_op_key(key)
    if message.update_id is not None:
        return balance.make_op_key("chat", message.chat_id, "update", message.update_id)
    return balance.new_op_key("chat:adhoc")


class TurnChargeStatus(StrEnum):
    CHARGED = "charged"
    UNREGISTERED = "unregistered"
    INSUFFICIENT = "insufficient"


@dataclass(frozen=True, slots=True)
class TurnCharge:
    status: TurnChargeStatus
    total_cost: int
    newly_charged: int = 0  # 本次真正扣掉的金币；重放的消息不计
    permission: int = 0
    info: str = ""
    balance_free: int = 0
    balance_paid: int = 0

    @property
    def balance_total(self) -> int:
        return self.balance_free + self.balance_paid


async def _charge_in_transaction(
    connection: AsyncConnection,
    user_id: int,
    messages: Sequence[TurnMessage],
) -> TurnCharge:
    total_cost = sum(message.cost for message in messages)
    newly_charged = 0
    for message in messages:
        op_key = message_op_key(message)
        result = await balance.debit(
            connection,
            user_id,
            message.cost,
            op_key=op_key,
            reason=CHAT_REASON,
        )
        if result.applied:
            newly_charged += message.cost
            # 只有这条消息真正扣了钱才贡献奖池；贡献自带 op_key，重放同样不会重复入账。
            await stake_reward_pool.credit_share_of_spend(
                connection,
                message.cost,
                spend_op_key=op_key,
            )

    # 用户行已被 debit 锁住，这里读到的是扣费后的最新值。
    row = await mysql_connection.fetch_one(
        "SELECT permission, coins, coins_paid, info FROM user WHERE id = %s",
        (user_id,),
        connection=connection,
    )
    if row is None:
        raise balance.UserNotFound(user_id)
    return TurnCharge(
        status=TurnChargeStatus.CHARGED,
        total_cost=total_cost,
        newly_charged=newly_charged,
        permission=row[0],
        info=row[3] or "",
        balance_free=row[1] or 0,
        balance_paid=row[2] or 0,
    )


async def charge_turn(user_id: int, messages: Sequence[TurnMessage]) -> TurnCharge:
    """为一轮对话扣费。

    返回的 `status`：

    - `CHARGED`：已扣费（或这几条消息之前已经扣过），可以进入本轮；同时带回用户的权限、
      个人信息与扣费后的余额。
    - `UNREGISTERED`：用户不存在，什么都没改动。
    - `INSUFFICIENT`：余额不足，整个事务回滚，没有扣费也没有奖池贡献。

    调用方必须检查 `status`，不是 `CHARGED` 就不能进入本轮。
    """
    total_cost = sum(message.cost for message in messages)

    async def work(connection: AsyncConnection) -> TurnCharge:
        return await _charge_in_transaction(connection, user_id, messages)

    try:
        return await balance.run_in_transaction(work)
    except balance.UserNotFound:
        return TurnCharge(TurnChargeStatus.UNREGISTERED, total_cost)
    except balance.InsufficientBalance as exc:
        return TurnCharge(
            TurnChargeStatus.INSUFFICIENT,
            total_cost,
            balance_free=exc.balance_free,
            balance_paid=exc.balance_paid,
        )
