"""MySQL 集成测试夹具。

未设置 TEST_MYSQL_URL 时，tests/integration 下的测试全部 skip。

可用的 fixture
--------------
mysql_database -> str
    一个全新的空库（唯一库名 `it_<hex>`）的 URL，测试结束后删除。
    适合测试迁移本身：`upgrade(mysql_database, "0016_add_ai_schedule_daily_limit")`。

migrated_database -> str
    已经 `upgrade head` 的库的 URL。会话内只跑一次全量迁移，之后每个测试都从模板
    复制一份（表结构含外键、迁移写入的种子数据与 alembic_version），测试之间互相隔离。

app_database -> str
    同 migrated_database，并且把应用的 `core.db` 引擎指向它；
    于是 `core.chat_records`、`core.process_user` 等真实代码路径都会读写这个库。
    结束时处置引擎并还原。想让应用连到别的状态的库（例如旧版本 schema），
    用 `with bind_app_engine(url): ...`。

测试里直接导入的帮助函数（来自 mysql_support）
    run(coro)                    在单个事件循环里运行协程并处置引擎；所有访问 core.db 的协程都应该这样跑
    fetch(url, sql, params)      执行查询，返回字典列表
    fetch_scalar(url, sql)       首行首列
    execute(url, *statements)    按顺序执行语句，一个事务（`%s` 占位符）
    upgrade / downgrade / stamp  alembic 命令，总是使用显式 URL
    current_versions(url)        alembic_version 中记录的版本号
    bind_app_engine(url)         上下文管理器，临时把 core.db 指向 `url`
    create_database()            另建一个库（会话结束时统一清理），需要时自行 drop_database

最小示例见 test_fixture_smoke.py。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 同目录的 mysql_support 供 conftest 与测试文件共用；显式加入路径，不依赖 pytest 的导入模式。
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import mysql_support as support  # noqa: E402

_SKIP_REASON = f"未设置 {support.TEST_MYSQL_URL_ENV}，跳过 MySQL 集成测试"


def pytest_configure(config):
    config.addinivalue_line("markers", "integration: 需要 TEST_MYSQL_URL 指向的真实 MySQL")


def pytest_collection_modifyitems(config, items):
    here = Path(__file__).resolve().parent
    skip = pytest.mark.skip(reason=_SKIP_REASON)
    for item in items:
        if here in Path(str(item.fspath)).resolve().parents:
            item.add_marker(pytest.mark.integration)
            if support.server_url() is None:
                item.add_marker(skip)


@pytest.fixture(autouse=True)
def _forbid_env_database():
    """集成测试期间，配置里的数据库地址是不可达的占位值。

    应用代码和 alembic 必须使用显式传入的测试 URL；任何回落到配置（也就是 `.env`）的
    路径都会连接失败，而不是悄悄连到真实数据库。
    """
    with support.use_forbidden_database_url():
        yield


@pytest.fixture(scope="session", autouse=True)
def _drop_leftover_databases():
    yield
    if support.server_url() is not None:
        support.drop_all_created_databases()


@pytest.fixture
def mysql_database():
    url = support.create_database()
    try:
        yield url
    finally:
        support.drop_database(url)


@pytest.fixture(scope="session")
def _migrated_template():
    """会话级模板：只迁移一次，之后按 SHOW CREATE TABLE 复制。"""
    if support.server_url() is None:
        pytest.skip(_SKIP_REASON)
    url = support.create_database()
    with support.use_forbidden_database_url():
        support.upgrade(url, "head")
    snapshot = support.snapshot_database(url)
    yield snapshot
    support.drop_database(url)


@pytest.fixture
def migrated_database(_migrated_template):
    url = support.create_database()
    try:
        support.restore_snapshot(url, _migrated_template)
        yield url
    finally:
        support.drop_database(url)


@pytest.fixture
def app_database(migrated_database):
    with support.bind_app_engine(migrated_database):
        yield migrated_database
