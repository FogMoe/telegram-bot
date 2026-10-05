"""MySQL 集成测试的支撑代码。

fixture 定义在同目录的 conftest.py；测试里直接用的函数从这里导入：

    from mysql_support import run, fetch, execute, upgrade

通用约定
--------
- 只有设置了环境变量 TEST_MYSQL_URL（例如 `mysql+asyncmy://root@127.0.0.1:3307`，
  不带库名）才会运行 tests/integration，否则整个目录 skip。
- 每个测试用唯一库名 `it_<hex>`，结束时删除；只会删除本模块自己创建的库。
- 应用代码与 alembic 只会拿到显式传入的测试 URL，永远不会回落到 `.env` 里的数据库。
- 会话统一追加 STRICT_TRANS_TABLES，不依赖服务器默认 sql_mode。
- asyncmy 的连接绑定创建它的事件循环，所以每个异步片段都通过 `run()` 在独立的事件循环里执行，
  结束时会处置应用引擎（`fogmoe_telegram_bot.core.db`）及额外传入的引擎。
"""

from __future__ import annotations

import asyncio
import importlib
import os
import uuid
from collections.abc import Coroutine, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alembic.config import Config
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[2]

TEST_MYSQL_URL_ENV = "TEST_MYSQL_URL"
DATABASE_PREFIX = "it_"
_STRICT_INIT_COMMAND = "SET SESSION sql_mode=CONCAT(@@SESSION.sql_mode, ',STRICT_TRANS_TABLES')"
# 配置里的库地址在集成测试期间被替换成它：任何漏网的回落都会连不上，而不是连到真实数据库。
UNREACHABLE_DATABASE_URL = "mysql+asyncmy://no-explicit-test-url@invalid.invalid:1/forbidden"

_created_databases: set[str] = set()


# ---------------------------------------------------------------------------
# 服务器与库
# ---------------------------------------------------------------------------


def server_url() -> URL | None:
    """TEST_MYSQL_URL 去掉库名与查询参数后的服务器地址；未设置返回 None。"""
    raw = os.environ.get(TEST_MYSQL_URL_ENV, "").strip()
    if not raw:
        return None
    url = make_url(raw)
    if url.get_backend_name() != "mysql" or url.get_driver_name() != "asyncmy":
        raise RuntimeError(f"{TEST_MYSQL_URL_ENV} 必须使用 mysql+asyncmy 驱动")
    return url.set(database=None, query={})


def database_url(name: str, *, base: URL | None = None) -> str:
    """指向库 `name` 的完整 URL（含 utf8mb4 与 strict 模式初始化）。"""
    base = base or server_url()
    if base is None:
        raise RuntimeError(f"未设置 {TEST_MYSQL_URL_ENV}")
    url = base.set(
        database=name,
        query={"charset": "utf8mb4", "init_command": _STRICT_INIT_COMMAND},
    )
    return url.render_as_string(hide_password=False)


def database_name(url: str) -> str:
    return make_url(url).database or ""


def unique_database_name() -> str:
    return f"{DATABASE_PREFIX}{uuid.uuid4().hex[:16]}"


def _admin_engine() -> AsyncEngine:
    base = server_url()
    assert base is not None
    return create_async_engine(base, poolclass=NullPool)


def create_database(name: str | None = None) -> str:
    """创建一个空库并返回它的 URL；库名登记后才允许被 drop_database 删除。"""
    name = name or unique_database_name()
    assert name.startswith(DATABASE_PREFIX), name
    _created_databases.add(name)

    async def _create() -> None:
        engine = _admin_engine()
        try:
            async with engine.connect() as connection:
                await connection.exec_driver_sql(
                    f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci"
                )
        finally:
            await engine.dispose()

    asyncio.run(_create())
    return database_url(name)


def drop_database(name_or_url: str) -> None:
    """删除本模块创建的库；对其他库一律拒绝。"""
    name = name_or_url if "://" not in name_or_url else database_name(name_or_url)
    if name not in _created_databases:
        raise RuntimeError(f"拒绝删除不是由集成测试创建的库: {name!r}")

    async def _drop() -> None:
        engine = _admin_engine()
        try:
            async with engine.connect() as connection:
                await connection.exec_driver_sql(f"DROP DATABASE IF EXISTS `{name}`")
        finally:
            await engine.dispose()

    asyncio.run(_drop())
    _created_databases.discard(name)


def drop_all_created_databases() -> None:
    for name in sorted(_created_databases):
        drop_database(name)


# ---------------------------------------------------------------------------
# 在单个事件循环里运行协程
# ---------------------------------------------------------------------------


async def _dispose_app_engine() -> None:
    from fogmoe_telegram_bot.core import db

    engine = db._ENGINE
    if engine is not None:
        await engine.dispose()


def run(coro: Coroutine[Any, Any, Any], *, engines: Iterable[AsyncEngine] = ()) -> Any:
    """在新的事件循环里运行 `coro`，结束后处置 `core.db` 引擎和 `engines`。

    连接池里的连接绑定创建它们的事件循环，所以每次 `run()` 之后必须把连接池清空，
    下一次 `run()` 才能在新的循环里重新建连。引擎对象本身保持不变。
    """

    async def _runner() -> Any:
        try:
            return await coro
        finally:
            await _dispose_app_engine()
            for engine in engines:
                await engine.dispose()

    return asyncio.run(_runner())


def _as_statement(item: str | tuple[str, Sequence[Any] | None]) -> tuple[str, Sequence[Any] | None]:
    if isinstance(item, str):
        return item, None
    return item[0], item[1]


def execute(url: str, *statements: str | tuple[str, Sequence[Any] | None]) -> None:
    """在库 `url` 上按顺序执行语句（`%s` 占位符），整体一个事务。"""

    async def _execute() -> None:
        engine = create_async_engine(url, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                for item in statements:
                    sql, params = _as_statement(item)
                    await connection.exec_driver_sql(sql, params)
        finally:
            await engine.dispose()

    asyncio.run(_execute())


def fetch(url: str, sql: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
    """在库 `url` 上执行查询，返回字典列表（列名按 SQL 里的写法）。"""

    async def _fetch() -> list[dict[str, Any]]:
        engine = create_async_engine(url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                result = await connection.exec_driver_sql(sql, params)
                return [dict(row) for row in result.mappings().all()]
        finally:
            await engine.dispose()

    return asyncio.run(_fetch())


def fetch_scalar(url: str, sql: str, params: Sequence[Any] | None = None) -> Any:
    rows = fetch(url, sql, params)
    return next(iter(rows[0].values())) if rows else None


# ---------------------------------------------------------------------------
# 库快照：把已迁移的库复制成新库，比每个测试都跑一遍全量迁移快
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DatabaseSnapshot:
    """某个库的表结构（SHOW CREATE TABLE，含外键）与有数据的表。"""

    source_database: str
    create_statements: tuple[tuple[str, str], ...]
    tables_with_rows: tuple[str, ...]


def snapshot_database(url: str) -> DatabaseSnapshot:
    async def _snapshot() -> DatabaseSnapshot:
        engine = create_async_engine(url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                result = await connection.exec_driver_sql(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = DATABASE() AND table_type = 'BASE TABLE' "
                    "ORDER BY table_name"
                )
                names = [row[0] for row in result.all()]
                statements: list[tuple[str, str]] = []
                with_rows: list[str] = []
                for name in names:
                    created = await connection.exec_driver_sql(f"SHOW CREATE TABLE `{name}`")
                    statements.append((name, created.one()[1]))
                    count = await connection.exec_driver_sql(f"SELECT COUNT(*) FROM `{name}`")
                    if count.scalar_one():
                        with_rows.append(name)
                return DatabaseSnapshot(
                    database_name(url), tuple(statements), tuple(with_rows)
                )
        finally:
            await engine.dispose()

    return asyncio.run(_snapshot())


def restore_snapshot(url: str, snapshot: DatabaseSnapshot) -> None:
    """在空库 `url` 里重建快照的表，并复制迁移写入的种子数据。"""

    async def _restore() -> None:
        engine = create_async_engine(url, poolclass=NullPool)
        target = database_name(url)
        try:
            async with engine.begin() as connection:
                await connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 0")
                for _, create_sql in snapshot.create_statements:
                    await connection.exec_driver_sql(create_sql)
                for name in snapshot.tables_with_rows:
                    await connection.exec_driver_sql(
                        f"INSERT INTO `{target}`.`{name}` "
                        f"SELECT * FROM `{snapshot.source_database}`.`{name}`"
                    )
                await connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 1")
        finally:
            await engine.dispose()

    asyncio.run(_restore())


# ---------------------------------------------------------------------------
# alembic（总是使用显式 URL）
# ---------------------------------------------------------------------------


def alembic_config(url: str) -> Config:
    """指向仓库迁移脚本的 alembic 配置，数据库固定为 `url`。

    不加载 alembic.ini，这样 env.py 不会改动 pytest 的日志配置；
    `cfg.attributes["db_url"]` 是 env.py 认可的显式 URL，优先于任何应用配置。
    """
    cfg = Config()
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.attributes["db_url"] = url
    return cfg


def head_revision() -> str:
    """迁移图当前唯一的 head；测试用它断言「已升级到最新」，新增 revision 时无需改测试。"""
    from alembic.script import ScriptDirectory

    heads = ScriptDirectory.from_config(alembic_config("mysql+asyncmy://unused/unused")).get_heads()
    assert len(heads) == 1, heads
    return heads[0]


def upgrade(url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(url), revision)


def downgrade(url: str, revision: str) -> None:
    command.downgrade(alembic_config(url), revision)


def stamp(url: str, revision: str | Sequence[str]) -> None:
    command.stamp(alembic_config(url), revision)  # type: ignore[arg-type]


def current_versions(url: str) -> list[str]:
    return sorted(row["version_num"] for row in fetch(url, "SELECT version_num FROM alembic_version"))


# ---------------------------------------------------------------------------
# 把应用的 core.db 指向测试库
# ---------------------------------------------------------------------------


def _config_modules() -> list[Any]:
    """应用的配置模块（应用代码与迁移脚本共用同一份）。"""
    return [importlib.import_module("fogmoe_telegram_bot.core.config")]


@contextmanager
def use_forbidden_database_url() -> Iterator[None]:
    """把配置里的数据库地址换成不可达的占位值，退出时还原。"""
    saved = [(module, module.SQLALCHEMY_DATABASE_URI) for module in _config_modules()]
    for module, _ in saved:
        module.SQLALCHEMY_DATABASE_URI = UNREACHABLE_DATABASE_URL
    try:
        yield
    finally:
        for module, value in saved:
            module.SQLALCHEMY_DATABASE_URI = value


@contextmanager
def bind_app_engine(url: str) -> Iterator[AsyncEngine]:
    """让 `core.db`（以及 core.sql / mysql_connection 等）在块内使用测试库 `url`。

    退出时处置引擎并还原 `core.db._ENGINE`；配置里的数据库地址同时指向 `url`，
    即使有代码绕过 `get_engine()` 自己建引擎，也只会连到测试库。
    """
    from fogmoe_telegram_bot.core import db

    previous_engine = db._ENGINE
    previous_loop = db._MAIN_LOOP
    saved = [(module, module.SQLALCHEMY_DATABASE_URI) for module in _config_modules()]
    engine = create_async_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=10)
    db._ENGINE = engine
    db._MAIN_LOOP = None
    for module, _ in saved:
        module.SQLALCHEMY_DATABASE_URI = url
    try:
        yield engine
    finally:
        asyncio.run(engine.dispose())
        db._ENGINE = previous_engine
        db._MAIN_LOOP = previous_loop
        for module, value in saved:
            module.SQLALCHEMY_DATABASE_URI = value
