"""Alembic 迁移共用的支撑代码。

放在 `modules/core/` 而不是 `alembic/versions/`：后者的每个 .py 都会被当成 revision 加载。
这里只依赖 alembic 与 SQLAlchemy，不 import `core.config`，避免迁移脚本被动读取 `.env`。

提供三类能力：

1. 数据库 URL 的优先级解析（`resolve_database_url`）。
2. 版本表宽度准备（`prepare_version_table` / `install_wide_version_table`）。
3. 可重入 DDL 助手（`add_columns_if_missing` 等）。

MySQL 的 DDL 会隐式提交：迁移跑到一半失败，会出现「DDL 已生效但 alembic_version 没更新」。
旧 revision 因此改为先查 information_schema 再执行，同一个 revision 可以安全地重跑。
离线模式（`--sql`）无法查询数据库，助手一律按「对象尚不存在」生成完整 SQL。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import URL, Connection

VERSION_TABLE = "alembic_version"
# 最长的 revision ID 是 37 个字符；留足余量，避免以后再踩宽度问题。
VERSION_NUM_WIDTH = 255

# 显式传入数据库 URL 的两个入口，优先级高于应用配置：
#   程序化调用：cfg.attributes["db_url"] = "mysql+asyncmy://..."
#   命令行：    alembic -x db_url=mysql+asyncmy://... upgrade head
URL_ATTRIBUTE = "db_url"
URL_X_ARGUMENT = "db_url"


def resolve_database_url(
    *,
    attributes: Mapping[str, Any],
    x_args: Mapping[str, str],
    app_url: Callable[[], str | None],
    ini_url: str | None,
) -> str:
    """按「显式传入 > 应用配置 > alembic.ini」的顺序决定迁移要连的数据库。

    只要显式传入了 URL，就不会读取应用配置，测试因此不可能回落到 `.env` 中的库。
    """
    for explicit in (attributes.get(URL_ATTRIBUTE), x_args.get(URL_X_ARGUMENT)):
        if explicit:
            if isinstance(explicit, URL):
                return explicit.render_as_string(hide_password=False)
            return str(explicit)

    try:
        configured = app_url()
    except Exception:
        configured = None
    return configured or ini_url or ""


# ---------------------------------------------------------------------------
# 版本表
# ---------------------------------------------------------------------------

_wide_version_table_installed = False


def install_wide_version_table(width: int = VERSION_NUM_WIDTH) -> None:
    """让 alembic 自己建出（或在 --sql 中输出）的版本表使用 VARCHAR(width)。

    alembic 默认 VARCHAR(32)，装不下本项目 37 位的 revision ID。这里借助 alembic 提供的
    `version_table_impl` 钩子覆盖 MySQL 实现，进程内只安装一次。
    """
    global _wide_version_table_installed
    if _wide_version_table_installed:
        return

    from alembic.ddl.mysql import MySQLImpl

    class WideVersionMySQLImpl(MySQLImpl):
        __dialect__ = "mysql"

        def version_table_impl(self, **kw: Any) -> sa.Table:
            table = super().version_table_impl(**kw)
            table.c.version_num.type = sa.String(width)
            return table

    _wide_version_table_installed = True


def prepare_version_table(
    connection: Connection,
    known_revisions: Iterable[str] = (),
    *,
    table: str = VERSION_TABLE,
    width: int = VERSION_NUM_WIDTH,
) -> None:
    """在运行任何迁移之前，把版本表准备成足够宽。

    - 不存在：创建 `version_num VARCHAR(width) NOT NULL` 主键表。
    - 已存在且更窄（alembic 旧默认 VARCHAR(32)）：原地加宽。
    - 旧的 VARCHAR(32) 表在非 strict 模式下可能已把超长 revision 截断成 32 位，
      若截断值恰好只对应一个已知 revision，就改写成完整 ID。

    调用方需要在之后自行提交事务，再让 alembic 接管连接。
    """
    current_width = connection.execute(
        sa.text(
            "SELECT character_maximum_length FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = :t "
            "AND column_name = 'version_num'"
        ),
        {"t": table},
    ).scalar()
    if current_width is None:
        connection.execute(
            sa.text(
                f"CREATE TABLE `{table}` ("
                f"`version_num` VARCHAR({width}) NOT NULL, "
                f"CONSTRAINT `{table}_pkc` PRIMARY KEY (`version_num`))"
            )
        )
    elif int(current_width) < width:
        connection.execute(
            sa.text(f"ALTER TABLE `{table}` MODIFY COLUMN `version_num` VARCHAR({width}) NOT NULL")
        )
    repair_truncated_versions(connection, known_revisions, table=table)


def repair_truncated_versions(
    connection: Connection,
    known_revisions: Iterable[str],
    *,
    table: str = VERSION_TABLE,
) -> list[tuple[str, str]]:
    """把被 VARCHAR(32) 截断的版本号还原成完整 revision ID，返回 (旧值, 新值) 列表。"""
    known = list(known_revisions)
    if not known:
        return []
    known_set = set(known)
    recorded = [
        row[0]
        for row in connection.execute(sa.text(f"SELECT `version_num` FROM `{table}`")).all()
    ]
    repaired: list[tuple[str, str]] = []
    for value in recorded:
        if value in known_set:
            continue
        candidates = [revision for revision in known if revision.startswith(value)]
        if len(candidates) != 1:
            continue
        connection.execute(
            sa.text(f"UPDATE `{table}` SET `version_num` = :new WHERE `version_num` = :old"),
            {"new": candidates[0], "old": value},
        )
        repaired.append((value, candidates[0]))
    return repaired


def prepare_version_table_offline(starting_revision: str | None) -> str | None:
    """离线 SQL 需要在脚本开头补充的语句，没有就返回 None。

    从空库开始（没有起点）时，alembic 自己生成的 CREATE TABLE 已经是宽版本表；
    从某个 revision 开始升级时，目标库里可能是窄表，需要先加宽。
    """
    if starting_revision is None:
        return None
    return f"ALTER TABLE `{VERSION_TABLE}` MODIFY COLUMN `version_num` VARCHAR({VERSION_NUM_WIDTH}) NOT NULL"


# ---------------------------------------------------------------------------
# 可重入 DDL
# ---------------------------------------------------------------------------


def is_offline() -> bool:
    return bool(op.get_context().as_sql)


def _query(sql: str, **params: Any) -> sa.engine.Result:
    return op.get_bind().execute(sa.text(sql), params)


def table_exists(table: str) -> bool:
    if is_offline():
        return False
    return bool(
        _query(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = :t",
            t=table,
        ).scalar()
    )


def column_info(table: str, column: str) -> dict[str, Any] | None:
    """返回列的 column_type / extra / is_nullable，列不存在（或离线模式）时返回 None。"""
    if is_offline():
        return None
    row = (
        _query(
            "SELECT column_type AS column_type, extra AS extra, is_nullable AS is_nullable "
            "FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c",
            t=table,
            c=column,
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def column_exists(table: str, column: str) -> bool:
    return column_info(table, column) is not None


def index_definitions(table: str) -> dict[str, tuple[bool, tuple[str, ...]]]:
    """{索引名: (是否唯一, 列元组)}，主键的索引名是 PRIMARY；离线模式返回空。"""
    if is_offline():
        return {}
    rows = _query(
        "SELECT index_name AS index_name, non_unique AS non_unique, "
        "column_name AS column_name FROM information_schema.statistics "
        "WHERE table_schema = DATABASE() AND table_name = :t "
        "ORDER BY index_name, seq_in_index",
        t=table,
    ).mappings()
    result: dict[str, tuple[bool, list[str]]] = {}
    for row in rows:
        unique, columns = result.setdefault(row["index_name"], (not row["non_unique"], []))
        columns.append(row["column_name"])
    return {name: (unique, tuple(columns)) for name, (unique, columns) in result.items()}


def primary_key_columns(table: str) -> tuple[str, ...]:
    definition = index_definitions(table).get("PRIMARY")
    return definition[1] if definition else ()


def has_unique_index_on(table: str, columns: Sequence[str]) -> bool:
    """表上是否已有恰好覆盖 `columns` 的主键或唯一索引（不论索引名）。"""
    wanted = tuple(columns)
    return any(unique and cols == wanted for unique, cols in index_definitions(table).values())


def index_exists(table: str, index: str) -> bool:
    return index in index_definitions(table)


def add_columns_if_missing(table: str, columns: Sequence[tuple[str, str]]) -> None:
    """为 `table` 补上缺失的列；`columns` 是 (列名, 列定义含 AFTER 等位置子句)。

    缺失的列合并成一条 ALTER，行为与原来的单条语句一致；已存在的列直接跳过。
    """
    missing = [(name, definition) for name, definition in columns if not column_exists(table, name)]
    if not missing:
        return
    clauses = ", ".join(f"ADD COLUMN `{name}` {definition}" for name, definition in missing)
    op.execute(f"ALTER TABLE `{table}` {clauses}")


def add_unique_key_if_missing(table: str, name: str, columns: Sequence[str]) -> None:
    """保证 `columns` 上存在唯一约束；已有同列的主键/唯一索引（任意名字）就跳过。"""
    if has_unique_index_on(table, columns):
        return
    column_list = ", ".join(f"`{column}`" for column in columns)
    op.execute(f"ALTER TABLE `{table}` ADD UNIQUE KEY `{name}` ({column_list})")


def drop_index_if_exists(table: str, name: str) -> None:
    if is_offline() or index_exists(table, name):
        op.execute(f"ALTER TABLE `{table}` DROP INDEX `{name}`")
