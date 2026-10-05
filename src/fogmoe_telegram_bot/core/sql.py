"""通用 SQL 助手。

只封装取值与执行，不承载任何业务语义；领域查询放在各自的 repository 模块。
"""

from collections.abc import Sequence
from typing import Any, Literal, overload

from sqlalchemy.engine import CursorResult, Row, RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from . import db
from .db import SqlParams

connect = db.connect
transaction = db.transaction
run_sync = db.run_sync

MYSQL_ERROR_DUPLICATE_KEY = 1062
MYSQL_ERROR_DEADLOCK = 1213


def mysql_error_code(exc: BaseException) -> int | None:
    """从驱动异常（或 SQLAlchemy 包装后的异常）里取出 MySQL 错误码，取不到返回 None。"""
    original = getattr(exc, "orig", None) or exc
    args = getattr(original, "args", ())
    if args and isinstance(args[0], int):
        return args[0]
    return None


def is_duplicate_key_error(exc: BaseException) -> bool:
    return mysql_error_code(exc) == MYSQL_ERROR_DUPLICATE_KEY


def is_deadlock_error(exc: BaseException) -> bool:
    """死锁时 MySQL 已回滚整个事务，调用方只能重跑整个事务。"""
    return mysql_error_code(exc) == MYSQL_ERROR_DEADLOCK


@overload
async def fetch_one(
    sql: str,
    params: SqlParams = None,
    *,
    mapping: Literal[False] = False,
    connection: AsyncConnection | None = None,
) -> Row[Any] | None: ...


@overload
async def fetch_one(
    sql: str,
    params: SqlParams = None,
    *,
    mapping: Literal[True],
    connection: AsyncConnection | None = None,
) -> RowMapping | None: ...


async def fetch_one(
    sql: str,
    params: SqlParams = None,
    *,
    mapping: bool = False,
    connection: AsyncConnection | None = None,
) -> Row[Any] | RowMapping | None:
    result = await db.exec_sql(sql, params, connection=connection)
    if mapping:
        return result.mappings().first()
    return result.fetchone()


@overload
async def fetch_all(
    sql: str,
    params: SqlParams = None,
    *,
    mapping: Literal[False] = False,
    connection: AsyncConnection | None = None,
) -> Sequence[Row[Any]]: ...


@overload
async def fetch_all(
    sql: str,
    params: SqlParams = None,
    *,
    mapping: Literal[True],
    connection: AsyncConnection | None = None,
) -> Sequence[RowMapping]: ...


async def fetch_all(
    sql: str,
    params: SqlParams = None,
    *,
    mapping: bool = False,
    connection: AsyncConnection | None = None,
) -> Sequence[Row[Any]] | Sequence[RowMapping]:
    result = await db.exec_sql(sql, params, connection=connection)
    if mapping:
        return result.mappings().all()
    return result.fetchall()


async def execute(
    sql: str,
    params: SqlParams = None,
    *,
    connection: AsyncConnection | None = None,
) -> int:
    if connection is None:
        async with transaction() as connection:
            result: CursorResult[Any] = await connection.exec_driver_sql(sql, params)
            return result.rowcount
    result = await connection.exec_driver_sql(sql, params)
    return result.rowcount
