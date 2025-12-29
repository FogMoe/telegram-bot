import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Iterable, Optional

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from . import config

_ENGINE: Optional[AsyncEngine] = None
_MAIN_LOOP: Optional[asyncio.AbstractEventLoop] = None


def get_engine() -> AsyncEngine:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = create_async_engine(
            config.SQLALCHEMY_DATABASE_URI,
            pool_pre_ping=True,
            pool_recycle=config.MYSQL_POOL_RECYCLE,
            pool_size=config.MYSQL_POOL_SIZE,
            max_overflow=config.MYSQL_MAX_OVERFLOW,
            connect_args={"connect_timeout": config.MYSQL_CONNECT_TIMEOUT},
        )
    return _ENGINE


def set_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _MAIN_LOOP
    _MAIN_LOOP = loop


@asynccontextmanager
async def connect() -> AsyncIterator[AsyncConnection]:
    engine = get_engine()
    async with engine.connect() as connection:
        yield connection


@asynccontextmanager
async def transaction() -> AsyncIterator[AsyncConnection]:
    engine = get_engine()
    async with engine.begin() as connection:
        yield connection


def run_sync(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        loop = _MAIN_LOOP
        if loop and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(coro, loop)
            return future.result()
        return asyncio.run(coro)
    raise RuntimeError("run_sync cannot be used inside a running event loop")


async def exec_sql(
    sql: str,
    params: Optional[Iterable[Any]] = None,
    *,
    connection: Optional[AsyncConnection] = None,
):
    if connection is None:
        async with connect() as connection:
            return await connection.exec_driver_sql(sql, params)
    return await connection.exec_driver_sql(sql, params)
