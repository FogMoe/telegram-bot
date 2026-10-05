"""RPG 的金币结算：回血、击败怪物、玩家对战。

每个操作在一个事务里完成余额变动与角色状态变化，余额一律走 `core.balance`，
op_key 由命令消息的位置（chat id 与 message id）派生：

- 回血 `rpg:heal:<chat>:<msg>`
- 击败怪物 `rpg:monster:<chat>:<msg>:reward`
- 玩家对战 `rpg:pvp:<chat>:<msg>:loss`（败者扣款）与 `rpg:pvp:<chat>:<msg>:win`（胜者入账）

同一条命令消息被重复投递时，op_key 已经记账：奖励与扣款不会再生效，调用方据返回值给出提示。
加锁顺序固定为「user 行（对战时按 id 升序）-> rpg_characters 行」。
"""

import math
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_telegram_bot.core import balance
from fogmoe_telegram_bot.core.command_identity import message_identity

from ..repositories import rpg as rpg_repository

HEAL_COST = 10
PVP_LOSS_RATE = 0.10  # 败者损失当前金币的 10%
PVP_WINNER_SHARE = 0.8  # 其中 80% 归胜者

HEAL_DONE = "done"
HEAL_FULL = "full"  # 生命值已满，没有扣费
HEAL_NO_CHARACTER = "no_character"
HEAL_INSUFFICIENT = "insufficient"
HEAL_REPLAY = "replay"  # 这条命令已经处理过，没有再扣费


def heal_op_key(chat_id: int, message_id: int) -> str:
    return balance.make_op_key("rpg", "heal", *message_identity(chat_id, message_id))


def monster_reward_op_key(chat_id: int, message_id: int) -> str:
    return balance.make_op_key("rpg", "monster", *message_identity(chat_id, message_id), "reward")


def pvp_loss_op_key(chat_id: int, message_id: int) -> str:
    return balance.make_op_key("rpg", "pvp", *message_identity(chat_id, message_id), "loss")


def pvp_win_op_key(chat_id: int, message_id: int) -> str:
    return balance.make_op_key("rpg", "pvp", *message_identity(chat_id, message_id), "win")


async def set_character_fields(
    connection: AsyncConnection, user_id: int, updates: dict
) -> int:
    """在调用方的事务里更新角色字段；字段名不合法或写入失败都抛异常，让事务回滚。"""
    return await rpg_repository.update_character_fields(connection, user_id, updates)


@dataclass(frozen=True, slots=True)
class HealResult:
    status: str
    max_hp: int = 0
    balance_total: int = 0  # 余额不足时是当前余额，否则是扣费后的余额


async def heal_for_coins(user_id: int, op_key: str) -> HealResult:
    """扣费并把生命值恢复到上限：同一个事务。余额不足或已满血时什么都不改。"""

    async def work(connection: AsyncConnection) -> HealResult:
        try:
            await balance.lock_user(connection, user_id)
        except balance.UserNotFound:
            return HealResult(HEAL_NO_CHARACTER)
        locked = await rpg_repository.lock_character_hp(connection, user_id)
        if locked is None:
            return HealResult(HEAL_NO_CHARACTER)
        hp, max_hp = locked
        if hp >= max_hp:
            return HealResult(HEAL_FULL, max_hp)
        try:
            charged = await balance.debit(
                connection, user_id, HEAL_COST, op_key=op_key, reason="rpg_heal"
            )
        except balance.InsufficientBalance as exc:
            return HealResult(HEAL_INSUFFICIENT, max_hp, exc.balance_total)
        if not charged.applied:
            return HealResult(HEAL_REPLAY, max_hp, charged.balance_total)
        await set_character_fields(connection, user_id, {"hp": max_hp})
        return HealResult(HEAL_DONE, max_hp, charged.balance_total)

    return await balance.run_in_transaction(work)


async def settle_monster_battle(
    user_id: int,
    *,
    chat_id: int,
    message_id: int,
    player_hp: float,
    won: bool,
    exp_reward: int,
    coin_reward: int,
) -> bool:
    """怪物战斗的结算：金币奖励、经验、生命值在同一个事务里。

    返回 False 表示这场战斗的奖励已经结算过（同一条命令被重复投递），本次没有改动任何数据。
    经验用增量写入，不依赖战斗开始时读到的旧值。
    """

    async def work(connection: AsyncConnection) -> bool:
        if won and coin_reward > 0:
            credited = await balance.credit(
                connection,
                user_id,
                coin_reward,
                op_key=monster_reward_op_key(chat_id, message_id),
                reason="rpg_monster",
            )
            if not credited.applied:
                return False
        await set_character_fields(connection, user_id, {"hp": player_hp})
        if won:
            await rpg_repository.add_experience(connection, user_id, exp_reward)
        return True

    return await balance.run_in_transaction(work)


@dataclass(frozen=True, slots=True)
class PvpSettlement:
    applied: bool  # False：这场战斗已经结算过，下面的数字是当时记录的金额
    coins_lost: int
    coins_to_winner: int


async def settle_player_battle(
    *,
    chat_id: int,
    message_id: int,
    winner_id: int,
    loser_id: int,
    exp_gain: int,
    hp_after: dict[int, float],
) -> PvpSettlement:
    """玩家对战的结算：败者扣款、胜者入账、胜者经验、双方生命值，同一个事务。

    扣款金额按锁住 user 行之后的最新余额计算，不用战斗开始时读到的旧余额。
    """
    loss_key = pvp_loss_op_key(chat_id, message_id)
    win_key = pvp_win_op_key(chat_id, message_id)

    async def work(connection: AsyncConnection) -> PvpSettlement:
        balances = {}
        for user_id in sorted({winner_id, loser_id}):
            balances[user_id] = await balance.lock_user(connection, user_id)

        recorded = await balance.get_operation(loss_key, connection=connection)
        if recorded is not None:
            paid = await balance.get_operation(win_key, connection=connection)
            return PvpSettlement(False, recorded.amount, 0 if paid is None else paid.amount)

        coins_lost = math.floor(balances[loser_id].total * PVP_LOSS_RATE)
        coins_to_winner = math.floor(coins_lost * PVP_WINNER_SHARE)
        if coins_lost > 0:
            await balance.debit(
                connection, loser_id, coins_lost, op_key=loss_key, reason="rpg_pvp_loss"
            )
        if coins_to_winner > 0:
            await balance.credit(
                connection, winner_id, coins_to_winner, op_key=win_key, reason="rpg_pvp_win"
            )
        await rpg_repository.add_experience(connection, winner_id, exp_gain)
        for user_id in sorted(hp_after):
            await set_character_fields(connection, user_id, {"hp": hp_after[user_id]})
        return PvpSettlement(True, coins_lost, coins_to_winner)

    return await balance.run_in_transaction(work)
