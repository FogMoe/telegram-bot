"""质押记录（`user_stakes`，以 user_id 为主键、每人至多一条）与回报率用到的总量。

奖池（`stake_reward_pool`）不在这里：它和余额一样走 `core.stake_reward_pool`。
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_telegram_bot.core import sql


@dataclass(frozen=True, slots=True)
class StakeRecord:
    stake_amount: int
    stake_time: datetime
    last_reward_time: datetime | None


async def get_stake(
    user_id: int, *, connection: AsyncConnection | None = None
) -> StakeRecord | None:
    row = await sql.fetch_one(
        "SELECT stake_amount, stake_time, last_reward_time FROM user_stakes WHERE user_id = %s",
        (user_id,),
        connection=connection,
    )
    if not row:
        return None
    return StakeRecord(
        stake_amount=int(row[0]),
        stake_time=row[1],
        last_reward_time=row[2],
    )


async def insert_stake(
    connection: AsyncConnection, user_id: int, amount: int, stake_time: datetime
) -> None:
    await connection.exec_driver_sql(
        "INSERT INTO user_stakes (user_id, stake_amount, stake_time) VALUES (%s, %s, %s)",
        (user_id, amount, stake_time),
    )


async def set_last_reward_time(
    connection: AsyncConnection, user_id: int, last_reward_time: datetime
) -> None:
    await connection.exec_driver_sql(
        "UPDATE user_stakes SET last_reward_time = %s WHERE user_id = %s",
        (last_reward_time, user_id),
    )


async def delete_stake(connection: AsyncConnection, user_id: int) -> None:
    await connection.exec_driver_sql(
        "DELETE FROM user_stakes WHERE user_id = %s",
        (user_id,),
    )


async def sum_user_coins(*, connection: AsyncConnection | None = None) -> Decimal:
    """全体用户的金币总量（免费加付费），没有用户时是 0。"""
    row = await sql.fetch_one("SELECT SUM(coins + coins_paid) FROM user", connection=connection)
    return Decimal(row[0]) if row and row[0] else Decimal(0)


async def sum_staked(*, connection: AsyncConnection | None = None) -> Decimal:
    """全体用户的质押总额，没有质押时是 0。"""
    row = await sql.fetch_one("SELECT SUM(stake_amount) FROM user_stakes", connection=connection)
    return Decimal(row[0]) if row and row[0] else Decimal(0)
