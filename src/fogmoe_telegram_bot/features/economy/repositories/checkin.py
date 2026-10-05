"""签到记录（`user_checkin`，每人一行：最近一次签到日与连续天数）。"""

from dataclasses import dataclass
from datetime import date

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_telegram_bot.core import sql


@dataclass(frozen=True, slots=True)
class CheckinRecord:
    last_checkin_date: date
    consecutive_days: int


async def get_checkin(
    user_id: int, *, connection: AsyncConnection | None = None
) -> CheckinRecord | None:
    row = await sql.fetch_one(
        "SELECT last_checkin_date, consecutive_days FROM user_checkin WHERE user_id = %s",
        (user_id,),
        connection=connection,
    )
    return None if row is None else CheckinRecord(row[0], int(row[1]))


async def save_checkin(
    connection: AsyncConnection, user_id: int, day: date, consecutive_days: int
) -> None:
    await connection.exec_driver_sql(
        """
        INSERT INTO user_checkin (user_id, last_checkin_date, consecutive_days)
        VALUES (%s, %s, %s)
        ON DUPLICATE KEY UPDATE last_checkin_date = VALUES(last_checkin_date), consecutive_days = VALUES(consecutive_days)
        """,
        (user_id, day, consecutive_days),
    )
