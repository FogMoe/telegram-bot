"""御神签记录（`user_omikuji`，主键 `(user_id, fortune_date)`：每人每天一条）。"""

from datetime import date

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_telegram_bot.core import sql


async def get_fortune(
    user_id: int, day: date, *, connection: AsyncConnection | None = None
) -> str | None:
    """用户这一天抽到的签文；还没抽过返回 None。"""
    row = await sql.fetch_one(
        "SELECT fortune FROM user_omikuji WHERE user_id = %s AND fortune_date = %s",
        (user_id, day),
        connection=connection,
    )
    return None if row is None else str(row[0])


async def save_fortune(connection: AsyncConnection, user_id: int, day: date, fortune: str) -> None:
    await connection.exec_driver_sql(
        "INSERT INTO user_omikuji (user_id, fortune_date, fortune) VALUES (%s, %s, %s) "
        "ON DUPLICATE KEY UPDATE fortune = VALUES(fortune)",
        (user_id, day, fortune),
    )
