"""每日抽奖的资格时间戳（`user_lottery`，每人一行：上一次抽奖的时间）。"""

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_telegram_bot.core import sql


async def get_last_lottery_date(
    user_id: int, *, connection: AsyncConnection | None = None
) -> datetime | None:
    row = await sql.fetch_one(
        "SELECT last_lottery_date FROM user_lottery WHERE user_id = %s",
        (user_id,),
        connection=connection,
    )
    return row[0] if row else None


async def save_last_lottery_date(
    connection: AsyncConnection, user_id: int, when: datetime
) -> None:
    await connection.exec_driver_sql(
        "INSERT INTO user_lottery (user_id, last_lottery_date) VALUES (%s, %s) "
        "ON DUPLICATE KEY UPDATE last_lottery_date = VALUES(last_lottery_date)",
        (user_id, when),
    )
