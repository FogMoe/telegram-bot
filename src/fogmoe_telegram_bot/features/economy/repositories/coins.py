"""赠送次数（`user_give_daily`）与富豪榜。收款人按名字解析用 `core.user_records.find_id_by_name`。"""

from dataclasses import dataclass
from datetime import date

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_telegram_bot.core import sql


@dataclass(frozen=True, slots=True)
class RichEntry:
    name: str
    coins_total: int


async def get_daily_give_count(
    connection: AsyncConnection, user_id: int, day: date
) -> int:
    row = await sql.fetch_one(
        "SELECT give_count FROM user_give_daily WHERE user_id = %s AND give_date = %s",
        (user_id, day),
        connection=connection,
    )
    return int(row[0]) if row else 0


async def increment_daily_give_count(
    connection: AsyncConnection, user_id: int, day: date
) -> None:
    await connection.exec_driver_sql(
        "INSERT INTO user_give_daily (user_id, give_date, give_count) VALUES (%s, %s, 1) "
        "ON DUPLICATE KEY UPDATE give_count = give_count + 1",
        (user_id, day),
    )


async def richest_users(limit: int, *, connection: AsyncConnection | None = None) -> list[RichEntry]:
    """按免费加付费金币总数从多到少排序的前 `limit` 个用户。"""
    rows = await sql.fetch_all(
        "SELECT name, (coins + coins_paid) AS coins_total FROM user "
        "ORDER BY coins_total DESC LIMIT %s",
        (limit,),
        connection=connection,
    )
    return [RichEntry(str(row[0]), int(row[1])) for row in rows]
