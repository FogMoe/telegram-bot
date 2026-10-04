"""迁移基线的不依赖数据库的检查：revision 图、离线 SQL、数据库 URL 优先级。

连接真实 MySQL 的迁移测试在 tests/integration/。
"""

import io
import sys
from argparse import Namespace
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine import make_url

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules.core import migration_support  # noqa: E402

# 只用来决定 SQL 方言，离线模式不会连接。
OFFLINE_URL = "mysql+asyncmy://user:secret@localhost/offline_only"


def _config(**kwargs) -> Config:
    cfg = Config(**kwargs)
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.attributes["db_url"] = OFFLINE_URL
    return cfg


def _offline_sql(revision_range: str) -> str:
    buffer = io.StringIO()
    command.upgrade(_config(output_buffer=buffer), revision_range, sql=True)
    return buffer.getvalue()


def test_migration_graph_has_a_single_head():
    script = ScriptDirectory.from_config(_config())

    assert len(script.get_heads()) == 1


def test_every_revision_id_fits_the_version_column():
    script = ScriptDirectory.from_config(_config())

    longest = max(len(rev.revision) for rev in script.walk_revisions())

    assert longest <= migration_support.VERSION_NUM_WIDTH
    # 默认的 VARCHAR(32) 装不下：这正是版本表需要预先加宽的原因。
    assert longest > 32


def test_offline_sql_from_base_creates_a_wide_version_table():
    sql = _offline_sql("head")

    assert "version_num VARCHAR(255) NOT NULL" in sql
    assert "version_num VARCHAR(32)" not in sql
    # 超长 revision 完整写入，没有被截断。
    assert "0014_add_ai_user_diary_page_index" in sql
    assert "0002_add_chat_records_last_rotated_at" in sql
    head = ScriptDirectory.from_config(_config()).get_heads()[0]
    assert f"UPDATE alembic_version SET version_num='{head}'" in sql


def test_offline_sql_from_a_revision_widens_the_existing_version_table_first():
    sql = _offline_sql("0016_add_ai_schedule_daily_limit:head")

    widen = sql.index("ALTER TABLE `alembic_version` MODIFY COLUMN `version_num` VARCHAR(255)")
    assert widen < sql.index("0017_schema_contracts")
    assert "CREATE TABLE alembic_version" not in sql


def test_offline_sql_contains_the_schema_contract_ddl():
    sql = _offline_sql("0016_add_ai_schedule_daily_limit:head")

    assert "AUTO_INCREMENT PRIMARY KEY" in sql
    assert "uq_chat_records_conversation_id" in sql
    assert "ADD PRIMARY KEY (`user_id`)" in sql


def test_offline_sql_contains_the_ledger_tables():
    sql = _offline_sql("0018_privacy_retention:head")

    assert "CREATE TABLE IF NOT EXISTS `coin_ledger`" in sql
    assert "UNIQUE KEY `uq_coin_ledger_op_key` (`op_key`)" in sql
    assert "CREATE TABLE IF NOT EXISTS `stake_pool_ledger`" in sql
    assert "CREATE TABLE IF NOT EXISTS `topup_requests`" in sql


class TestResolveDatabaseUrl:
    @staticmethod
    def _resolve(*, attributes=None, x_args=None, app_url=lambda: "app://url", ini_url="ini://url"):
        return migration_support.resolve_database_url(
            attributes=attributes or {},
            x_args=x_args or {},
            app_url=app_url,
            ini_url=ini_url,
        )

    def test_attribute_beats_x_argument_and_application_config(self):
        url = self._resolve(attributes={"db_url": "attr://url"}, x_args={"db_url": "x://url"})

        assert url == "attr://url"

    def test_x_argument_beats_application_config(self):
        assert self._resolve(x_args={"db_url": "x://url"}) == "x://url"

    def test_explicit_url_never_consults_application_config(self):
        def app_url():
            raise AssertionError("explicit URL must not read the application config")

        assert self._resolve(attributes={"db_url": "attr://url"}, app_url=app_url) == "attr://url"

    def test_application_config_is_the_default(self):
        assert self._resolve() == "app://url"

    def test_ini_url_is_used_when_application_config_is_unavailable(self):
        def app_url():
            raise RuntimeError("no config")

        assert self._resolve(app_url=app_url) == "ini://url"

    def test_url_objects_keep_their_password(self):
        url = self._resolve(attributes={"db_url": make_url(OFFLINE_URL)})

        assert "secret" in url
        assert make_url(url).password == "secret"


def test_env_reads_x_argument_from_the_command_line():
    cfg = _config(cmd_opts=Namespace(x=["db_url=mysql+asyncmy://cli@localhost/from_cli"]))
    cfg.attributes.pop("db_url")

    sql = io.StringIO()
    cfg.output_buffer = sql
    # 离线模式不会连接数据库；能走完说明 -x 参数被 env.py 接受。
    command.upgrade(cfg, "head", sql=True)

    assert "CREATE TABLE alembic_version" in sql.getvalue()


@pytest.mark.parametrize("starting_revision", [None, "0016_add_ai_schedule_daily_limit"])
def test_offline_prelude_only_applies_when_starting_from_a_revision(starting_revision):
    prelude = migration_support.prepare_version_table_offline(starting_revision)

    assert (prelude is None) == (starting_revision is None)
