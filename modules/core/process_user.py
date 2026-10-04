import logging

from sqlalchemy.ext.asyncio import AsyncConnection

from . import mysql_connection, user_records
# 套餐常量与推导规则的定义在 balance 里，这里原样转出，旧代码的 import 路径不变。
from .balance import USER_PLAN_ADMIN as USER_PLAN_ADMIN
from .balance import USER_PLAN_FREE as USER_PLAN_FREE
from .balance import USER_PLAN_PAID as USER_PLAN_PAID
from .balance import resolve_user_plan as resolve_user_plan

logger = logging.getLogger(__name__)


# 用户基础读取的 SQL 在 core.user_records；这里的同名函数是历史 import 路径，只做转发。


async def get_user_coin_balances(user_id, *, connection=None) -> tuple[int, int]:
    return await user_records.get_coin_balances(user_id, connection=connection)


async def get_user_total_coins(user_id, *, connection=None) -> int:
    coins_free, coins_paid = await get_user_coin_balances(
        user_id,
        connection=connection,
    )
    return coins_free + coins_paid


async def user_exists(user_id):
    return await user_records.check_user_exists(user_id)


async def async_user_exists(user_id):
    return await user_exists(user_id)


async def get_user_personal_info(user_id: int) -> str:
    return await user_records.get_personal_info(user_id)


async def get_user_coins(user_id: int) -> int:
    return await get_user_total_coins(user_id)


async def async_get_user_coins(user_id: int) -> int:
    return await get_user_coins(user_id)


async def get_user_affection(user_id: int) -> int:
    row = await mysql_connection.fetch_one(
        "SELECT affection FROM ai_user_affection WHERE user_id = %s",
        (user_id,),
    )
    return row[0] if row else 0


def get_user_affection_sync(user_id: int) -> int:
    # 同步边界：只能在没有事件循环的线程里调用，主路径用 get_user_affection；见 docs/runtime.md。
    return mysql_connection.run_sync(get_user_affection(user_id))


async def update_user_affection(
    user_id: int,
    delta: int,
    *,
    connection: AsyncConnection | None = None,
) -> int:
    """调整好感度（单次变化限制在 ±10，总值限制在 ±100），返回调整后的值。

    传入 `connection` 时在调用方的事务里执行，不自己开事务；扣费与好感度变化要一起提交时用它。
    """
    delta = int(delta)
    if delta > 10:
        delta = 10
    elif delta < -10:
        delta = -10

    if connection is not None:
        return await _apply_affection_delta(connection, user_id, delta)
    async with mysql_connection.transaction() as own_connection:
        return await _apply_affection_delta(own_connection, user_id, delta)


async def _apply_affection_delta(connection: AsyncConnection, user_id: int, delta: int) -> int:
    row = await mysql_connection.fetch_one(
        "SELECT affection FROM ai_user_affection WHERE user_id = %s FOR UPDATE",
        (user_id,),
        connection=connection,
    )
    current = row[0] if row else 0
    updated = max(-100, min(100, current + delta))

    if row:
        await connection.exec_driver_sql(
            "UPDATE ai_user_affection SET affection = %s WHERE user_id = %s",
            (updated, user_id),
        )
    else:
        await connection.exec_driver_sql(
            "INSERT INTO ai_user_affection (user_id, affection) VALUES (%s, %s)",
            (user_id, updated),
        )
    return updated


def update_user_affection_sync(user_id: int, delta: int) -> int:
    # 同步边界：只能在没有事件循环的线程里调用，主路径用 update_user_affection；见 docs/runtime.md。
    return mysql_connection.run_sync(update_user_affection(user_id, delta))


async def async_get_user_affection(user_id: int) -> int:
    return await get_user_affection(user_id)


async def async_update_user_affection(user_id: int, delta: int) -> int:
    return await update_user_affection(user_id, delta)


async def get_user_permission(user_id: int) -> int:
    return await user_records.get_permission(user_id)


async def async_get_user_permission(user_id: int) -> int:
    return await get_user_permission(user_id)


async def get_user_impression(user_id: int) -> str:
    row = await mysql_connection.fetch_one(
        "SELECT impression FROM ai_user_affection WHERE user_id = %s",
        (user_id,),
    )
    if row and row[0] is not None:
        return row[0]
    return ""


async def update_user_impression(user_id: int, impression: str) -> str:
    text = (impression or "").strip()
    async with mysql_connection.transaction() as connection:
        row = await mysql_connection.fetch_one(
            "SELECT impression FROM ai_user_affection WHERE user_id = %s",
            (user_id,),
            connection=connection,
        )
        if row:
            await connection.exec_driver_sql(
                "UPDATE ai_user_affection SET impression = %s WHERE user_id = %s",
                (text, user_id),
            )
        else:
            await connection.exec_driver_sql(
                "INSERT INTO ai_user_affection (user_id, affection, impression) VALUES (%s, %s, %s)",
                (user_id, 0, text),
            )
    return text


def update_user_impression_sync(user_id: int, impression: str) -> str:
    # 同步边界：只能在没有事件循环的线程里调用，主路径用 update_user_impression；见 docs/runtime.md。
    return mysql_connection.run_sync(update_user_impression(user_id, impression))


async def async_get_user_impression(user_id: int) -> str:
    return await get_user_impression(user_id)
