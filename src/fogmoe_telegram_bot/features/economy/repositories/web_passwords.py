"""网页密码哈希（`web_password`，每个用户一行）。存的只有哈希，明文密码不会到这里。"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_telegram_bot.core import sql


@dataclass(frozen=True, slots=True)
class WebPasswordRecord:
    password_hash: str
    created_at: datetime
    updated_at: datetime


async def get_web_password(
    user_id: int, *, connection: AsyncConnection | None = None
) -> WebPasswordRecord | None:
    row = await sql.fetch_one(
        "SELECT password, created_at, updated_at FROM web_password WHERE user_id = %s",
        (user_id,),
        connection=connection,
    )
    if row is None:
        return None
    return WebPasswordRecord(str(row[0]), row[1], row[2])


async def save_web_password(connection: AsyncConnection, user_id: int, password_hash: str) -> None:
    """设置或更新（已有记录时只更新哈希）。"""
    await connection.exec_driver_sql(
        """
        INSERT INTO web_password (user_id, password)
        VALUES (%s, %s)
        ON DUPLICATE KEY UPDATE password = VALUES(password)
        """,
        (user_id, password_hash),
    )
