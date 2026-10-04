"""充值相关的持久化：卡密（`redemption_codes`）、管理员充值请求（`topup_requests`）与 /recharge 的禁用截止时间。

用户名读取用 `core.user_records.get_name`；入账走 `core.balance`，不在这里。
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncConnection

from core import sql

TOPUP_STATUS_PENDING = "pending"


@dataclass(frozen=True, slots=True)
class RedemptionCode:
    id: int
    code: str
    amount: int
    is_used: bool
    used_by: int | None
    used_at: datetime | None


@dataclass(frozen=True, slots=True)
class TopupRequest:
    id: int
    user_id: int
    coins: int
    price_cents: int
    status: str


# ---------------------------------------------------------------------------
# 卡密
# ---------------------------------------------------------------------------


async def lock_redemption_code(connection: AsyncConnection, code: str) -> RedemptionCode | None:
    """按卡密取行并加锁（`FOR UPDATE`）：同一张卡密的并发兑换在这里串行。"""
    row = await sql.fetch_one(
        "SELECT id, code, amount, is_used, used_by, used_at FROM redemption_codes "
        "WHERE code = %s FOR UPDATE",
        (code,),
        connection=connection,
    )
    if row is None:
        return None
    return RedemptionCode(
        id=int(row[0]),
        code=str(row[1]),
        amount=int(row[2]),
        is_used=bool(row[3]),
        used_by=None if row[4] is None else int(row[4]),
        used_at=row[5],
    )


async def mark_code_used(
    connection: AsyncConnection, code_id: int, user_id: int, used_at: datetime
) -> None:
    await connection.exec_driver_sql(
        "UPDATE redemption_codes SET is_used = TRUE, used_by = %s, used_at = %s WHERE id = %s",
        (user_id, used_at, code_id),
    )


async def code_exists(connection: AsyncConnection, code: str) -> bool:
    row = await sql.fetch_one(
        "SELECT id FROM redemption_codes WHERE code = %s",
        (code,),
        connection=connection,
    )
    return row is not None


async def insert_code(connection: AsyncConnection, code: str, amount: int) -> None:
    await connection.exec_driver_sql(
        "INSERT INTO redemption_codes (code, amount) VALUES (%s, %s)",
        (code, amount),
    )


# ---------------------------------------------------------------------------
# 充值请求
# ---------------------------------------------------------------------------


async def insert_topup_request(
    connection: AsyncConnection, user_id: int, coins: int, price_cents: int
) -> int:
    """记录一条 pending 的充值请求并返回它的 id。"""
    result = await connection.exec_driver_sql(
        "INSERT INTO topup_requests (user_id, coins, price_cents) VALUES (%s, %s, %s)",
        (user_id, coins, price_cents),
    )
    return int(result.lastrowid)


async def get_topup_request(
    request_id: int, *, connection: AsyncConnection | None = None
) -> TopupRequest | None:
    row = await sql.fetch_one(
        "SELECT id, user_id, coins, price_cents, status FROM topup_requests WHERE id = %s",
        (request_id,),
        connection=connection,
    )
    if row is None:
        return None
    return TopupRequest(
        id=int(row[0]),
        user_id=int(row[1]),
        coins=int(row[2]),
        price_cents=int(row[3]),
        status=str(row[4]),
    )


async def delete_pending_topup_request(connection: AsyncConnection, request_id: int) -> int:
    """删除仍是 pending 的请求，返回影响行数；已经被处理过的请求不受影响。"""
    result = await connection.exec_driver_sql(
        "DELETE FROM topup_requests WHERE id = %s AND status = 'pending'",
        (request_id,),
    )
    return int(result.rowcount)


async def claim_pending_topup_request(
    connection: AsyncConnection,
    request_id: int,
    new_status: str,
    decided_at: datetime,
    decided_by: int,
) -> bool:
    """`pending` 只能转换一次：影响行数为 1 才算占住了这次转换。"""
    result = await connection.exec_driver_sql(
        "UPDATE topup_requests SET status = %s, decided_at = %s, decided_by = %s "
        "WHERE id = %s AND status = 'pending'",
        (new_status, decided_at, decided_by, request_id),
    )
    return result.rowcount == 1


async def lock_topup_status(connection: AsyncConnection, request_id: int) -> str | None:
    """锁定读取请求的当前状态；事务里更早的普通读可能停留在旧快照。"""
    row = await sql.fetch_one(
        "SELECT status FROM topup_requests WHERE id = %s FOR UPDATE",
        (request_id,),
        connection=connection,
    )
    return None if row is None else str(row[0])


# ---------------------------------------------------------------------------
# /recharge 禁用
# ---------------------------------------------------------------------------


async def get_recharge_blocked_until(
    user_id: int, *, connection: AsyncConnection | None = None
) -> datetime | None:
    row = await sql.fetch_one(
        "SELECT recharge_blocked_until FROM user WHERE id = %s",
        (user_id,),
        connection=connection,
    )
    if not row:
        return None
    return row[0] or None


async def set_recharge_blocked_until(
    connection: AsyncConnection, user_id: int, blocked_until: datetime
) -> None:
    await connection.exec_driver_sql(
        "UPDATE user SET recharge_blocked_until = %s WHERE id = %s",
        (blocked_until, user_id),
    )
