"""商店购买会改动的 user 列：永久记忆上限与权限等级（读权限用 `core.user_records.get_permission`）。"""

from sqlalchemy.ext.asyncio import AsyncConnection

from core import sql


async def get_permanent_records_limit(connection: AsyncConnection, user_id: int) -> int | None:
    row = await sql.fetch_one(
        "SELECT permanent_records_limit FROM user WHERE id = %s",
        (user_id,),
        connection=connection,
    )
    return None if row is None else int(row[0])


async def increase_permanent_records_limit(
    connection: AsyncConnection, user_id: int, amount: int
) -> None:
    await connection.exec_driver_sql(
        "UPDATE user SET permanent_records_limit = permanent_records_limit + %s WHERE id = %s",
        (amount, user_id),
    )


async def set_permission(connection: AsyncConnection, user_id: int, level: int) -> None:
    await connection.exec_driver_sql(
        "UPDATE user SET permission = %s WHERE id = %s",
        (level, user_id),
    )
