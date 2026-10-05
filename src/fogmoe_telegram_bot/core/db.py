import asyncio
from collections.abc import AsyncIterator, Coroutine, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from . import config

# exec_driver_sql 接受的位置参数（序列）或命名参数（映射）。
SqlParams = Sequence[Any] | Mapping[str, Any] | None

_ENGINE: AsyncEngine | None = None
_MAIN_LOOP: asyncio.AbstractEventLoop | None = None


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


async def dispose_engine() -> None:
    """关闭连接池并丢弃引擎（进程停止时调用）；之后再使用会按当时的配置重新创建。"""
    global _ENGINE
    engine, _ENGINE = _ENGINE, None
    if engine is not None:
        await engine.dispose()


def run_sync[T](coro: Coroutine[Any, Any, T]) -> T:
    """从**没有运行事件循环的同步代码**里执行协程；主路径不使用它。

    只允许存在于明确的同步边界：`process_user.*_sync` 兼容包装、`group_chat_history.get_group_context`
    这类历史遗留的同步入口。在运行着事件循环的线程里调用会抛 RuntimeError；
    在别的线程里调用会把协程投递回主循环并阻塞等待，因此不要放进事件循环的回调或 async 工具里。
    AI 对话、工具和后台任务都应当直接 `await` 对应的 async 函数，见 docs/runtime.md。
    """
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
    params: SqlParams = None,
    *,
    connection: AsyncConnection | None = None,
) -> CursorResult[Any]:
    if connection is None:
        async with connect() as connection:
            return await connection.exec_driver_sql(sql, params)
    return await connection.exec_driver_sql(sql, params)
