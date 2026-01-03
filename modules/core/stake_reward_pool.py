from decimal import Decimal, ROUND_DOWN

from . import mysql_connection

POOL_ROW_ID = 1
POOL_RATE = Decimal("0.2")
POOL_QUANT = Decimal("0.01")


def _normalize_amount(amount) -> Decimal:
    if amount is None:
        return Decimal("0")
    value = amount if isinstance(amount, Decimal) else Decimal(str(amount))
    if value <= 0:
        return Decimal("0")
    return value.quantize(POOL_QUANT, rounding=ROUND_DOWN)


def calculate_pool_add(cost: int) -> Decimal:
    if cost <= 0:
        return Decimal("0")
    return _normalize_amount(Decimal(cost) * POOL_RATE)


async def _ensure_pool_row(*, connection=None) -> None:
    await mysql_connection.execute(
        "INSERT INTO stake_reward_pool (id, balance) VALUES (%s, 0) "
        "ON DUPLICATE KEY UPDATE balance = balance",
        (POOL_ROW_ID,),
        connection=connection,
    )


async def get_pool_balance(*, connection=None, for_update: bool = False) -> Decimal:
    await _ensure_pool_row(connection=connection)
    sql = "SELECT balance FROM stake_reward_pool WHERE id = %s"
    if for_update:
        sql += " FOR UPDATE"
    row = await mysql_connection.fetch_one(
        sql,
        (POOL_ROW_ID,),
        connection=connection,
    )
    if not row or row[0] is None:
        return Decimal("0")
    return Decimal(str(row[0]))


async def add_to_pool(amount, *, connection=None) -> Decimal:
    amount = _normalize_amount(amount)
    if amount <= 0:
        return Decimal("0")
    if connection is None:
        async with mysql_connection.transaction() as connection:
            return await add_to_pool(amount, connection=connection)
    await _ensure_pool_row(connection=connection)
    await mysql_connection.execute(
        "UPDATE stake_reward_pool SET balance = balance + %s WHERE id = %s",
        (amount, POOL_ROW_ID),
        connection=connection,
    )
    return amount


async def subtract_from_pool(amount, *, connection=None) -> Decimal:
    amount = _normalize_amount(amount)
    if amount <= 0:
        return Decimal("0")
    if connection is None:
        async with mysql_connection.transaction() as connection:
            return await subtract_from_pool(amount, connection=connection)
    await _ensure_pool_row(connection=connection)
    await mysql_connection.execute(
        "UPDATE stake_reward_pool SET balance = balance - %s WHERE id = %s",
        (amount, POOL_ROW_ID),
        connection=connection,
    )
    return amount
