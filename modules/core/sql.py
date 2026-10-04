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
