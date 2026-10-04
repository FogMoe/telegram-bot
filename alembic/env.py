from __future__ import annotations

import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from alembic.script import ScriptDirectory

# Ensure project root is on sys.path so `modules` can be imported.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from modules.core import migration_support  # noqa: E402

config = context.config

if config.config_file_name is not None:
    # 不要禁用已有 logger：测试进程里会反复程序化调用 alembic。
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# No SQLAlchemy models in use yet.
target_metadata = None

# 版本表必须在第一个超长 revision 被记录之前就是宽的，见 migration_support。
migration_support.install_wide_version_table()


def _app_database_url() -> str | None:
    from modules.core.config import SQLALCHEMY_DATABASE_URI

    return SQLALCHEMY_DATABASE_URI


def get_url() -> str:
    """数据库 URL 优先级：显式传入 > 应用配置 > alembic.ini。

    显式传入有两种方式（都不会触碰应用配置，也就不会回落到 .env 里的库）：
    `config.attributes["db_url"]`（程序化调用，测试使用）与 `-x db_url=...`（命令行）。
    """
    return migration_support.resolve_database_url(
        attributes=config.attributes,
        x_args=context.get_x_argument(as_dictionary=True),
        app_url=_app_database_url,
        ini_url=config.get_main_option("sqlalchemy.url"),
    )


def run_migrations_offline() -> None:
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        transaction_per_migration=True,
    )

    prelude = migration_support.prepare_version_table_offline(
        context.get_starting_revision_argument()
    )
    if prelude:
        context.execute(prelude)

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    known_revisions = [
        revision.revision for revision in ScriptDirectory.from_config(config).walk_revisions()
    ]
    migration_support.prepare_version_table(connection, known_revisions)
    # 版本表的 DDL/修复已经落盘；提交后 alembic 才会自己管理每个 revision 的事务。
    connection.commit()

    # 每个 revision 单独提交：版本号更新紧跟在该 revision 的 DDL 之后，
    # 失败时不会把前面已成功的 revision 一起回滚。
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        transaction_per_migration=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        url=get_url(),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
