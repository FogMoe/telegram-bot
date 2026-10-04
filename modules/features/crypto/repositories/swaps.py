"""$FOGMOE 兑换请求（`token_swap_requests`）。请求由运营在链上处理，状态为 `pending` 的请求是待处理的。"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncConnection

from core import sql


@dataclass(frozen=True, slots=True)
class PendingSwap:
    amount: int
    wallet_address: str
    request_time: datetime


async def get_pending_swap(
    user_id: int, *, connection: AsyncConnection | None = None
) -> PendingSwap | None:
    """用户最近一条待处理的兑换请求；没有返回 None。"""
    row = await sql.fetch_one(
        "SELECT amount, wallet_address, request_time FROM token_swap_requests "
        "WHERE user_id = %s AND status = 'pending' ORDER BY request_time DESC LIMIT 1",
        (user_id,),
        connection=connection,
    )
    if row is None:
        return None
    return PendingSwap(int(row[0]), str(row[1]), row[2])


async def insert_swap_request(
    connection: AsyncConnection,
    user_id: int,
    username: str,
    wallet_address: str,
    amount: int,
) -> None:
    await connection.exec_driver_sql(
        """
            INSERT INTO token_swap_requests (user_id, username, wallet_address, amount)
            VALUES (%s, %s, %s, %s)
            """,
        (user_id, username, wallet_address, amount),
    )
