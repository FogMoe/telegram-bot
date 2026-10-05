"""user 表的基础读写：跨功能共享的用户查询。

读取接受可选的 `connection`，传入时在调用方的事务里执行（拿到 user 行锁之后的第一次一致性读
能看到上一个持锁者提交的值）；写入必须由调用方的事务提供 `connection`。
余额变动不在这里：金币只通过 `core.balance` 变动，这里只读余额。
"""

from sqlalchemy.ext.asyncio import AsyncConnection

from .sql import fetch_one


async def check_user_exists(user_id: int, *, connection: AsyncConnection | None = None) -> bool:
    row = await fetch_one("SELECT id FROM user WHERE id = %s", (user_id,), connection=connection)
    return row is not None


async def async_check_user_exists(user_id: int) -> bool:
    return await check_user_exists(user_id)


async def get_coin_balances(
    user_id: int, *, connection: AsyncConnection | None = None
) -> tuple[int, int]:
    """(免费金币, 付费金币)；用户不存在时是 (0, 0)。"""
    row = await fetch_one(
        "SELECT coins, coins_paid FROM user WHERE id = %s",
        (user_id,),
        connection=connection,
    )
    if not row:
        return 0, 0
    return row[0] or 0, row[1] or 0


async def get_permission(user_id: int, *, connection: AsyncConnection | None = None) -> int:
    """权限等级；用户不存在时是 0。"""
    row = await fetch_one(
        "SELECT permission FROM user WHERE id = %s",
        (user_id,),
        connection=connection,
    )
    return row[0] if row else 0


async def get_personal_info(user_id: int) -> str:
    row = await fetch_one("SELECT info FROM user WHERE id = %s", (user_id,))
    if not row or row[0] is None or row[0] == "":
        return ""
    return str(row[0])


async def get_name(user_id: int, *, connection: AsyncConnection | None = None) -> str | None:
    row = await fetch_one("SELECT name FROM user WHERE id = %s", (user_id,), connection=connection)
    return None if row is None else str(row[0])


async def find_id_by_name(name: str, *, connection: AsyncConnection | None = None) -> int | None:
    row = await fetch_one("SELECT id FROM user WHERE name = %s", (name,), connection=connection)
    return None if row is None else int(row[0])


async def create_user(connection: AsyncConnection, user_id: int, name: str) -> None:
    """开户：余额为 0（开户奖励由调用方走余额服务入账）。已存在时抛 `IntegrityError`。"""
    await connection.exec_driver_sql(
        "INSERT INTO user (id, name, coins) VALUES (%s, %s, 0)",
        (user_id, name),
    )
