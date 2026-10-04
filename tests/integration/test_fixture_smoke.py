"""夹具的最小用法示例，同时验证夹具自身的隔离保证。"""

import pytest
from mysql_support import database_name, execute, fetch_scalar, run


def test_app_database_binds_the_application_engine(app_database):
    from core import config
    from core.sql import fetch_one

    async def current_database():
        row = await fetch_one("SELECT DATABASE()")
        return row[0]

    assert run(current_database()) == database_name(app_database)
    assert config.SQLALCHEMY_DATABASE_URI == app_database


def test_application_code_never_falls_back_to_the_configured_database(mysql_database):
    """不用 app_database 时，配置里的库地址是不可达的占位值，而不是 .env 里的真实地址。"""
    from core import config

    assert "invalid.invalid" in config.SQLALCHEMY_DATABASE_URI


@pytest.mark.parametrize("round_number", [1, 2])
def test_each_test_gets_an_isolated_migrated_database(app_database, round_number):
    # 两个参数化用例各自拿到全新的库：第二个用例看不到第一个用例写入的行。
    assert fetch_scalar(app_database, "SELECT COUNT(*) FROM `user`") == 0
    execute(app_database, ("INSERT INTO `user` (id, name) VALUES (%s, 'someone')", (round_number,)))
    assert fetch_scalar(app_database, "SELECT COUNT(*) FROM `user`") == 1


def test_migrated_database_keeps_seed_rows_written_by_migrations(migrated_database):
    assert fetch_scalar(migrated_database, "SELECT COUNT(*) FROM stake_reward_pool") == 1
