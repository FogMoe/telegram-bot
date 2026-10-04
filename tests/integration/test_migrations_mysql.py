"""在真实 MySQL 上验证迁移基线：版本表宽度、可重入、部分失败恢复、显式 URL。"""

from argparse import Namespace

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from legacy_data import first_message_content, seed_duplicate_rows
from mysql_support import (
    PROJECT_ROOT,
    alembic_config,
    current_versions,
    downgrade,
    execute,
    fetch,
    fetch_scalar,
    upgrade,
)

HEAD = "0019_coin_ledger"
REV_0016 = "0016_add_ai_schedule_daily_limit"
REV_0017 = "0017_schema_contracts"
LONG_REVISIONS = [
    "0002_add_chat_records_last_rotated_at",
    "0007_add_user_permanent_records_limit",
    "0014_add_ai_user_diary_page_index",
]


def schema_signature(url: str) -> dict[str, list[dict]]:
    """库的结构快照（不含 alembic_version）：列、索引、外键。"""
    return {
        "columns": fetch(
            url,
            "SELECT table_name AS t, column_name AS c, ordinal_position AS pos, "
            "column_type AS ty, is_nullable AS nullable, column_default AS dflt, extra AS extra "
            "FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name <> 'alembic_version' "
            "ORDER BY table_name, ordinal_position",
        ),
        "indexes": fetch(
            url,
            "SELECT table_name AS t, index_name AS idx, seq_in_index AS seq, "
            "column_name AS c, non_unique AS non_unique FROM information_schema.statistics "
            "WHERE table_schema = DATABASE() AND table_name <> 'alembic_version' "
            "ORDER BY table_name, index_name, seq_in_index",
        ),
        "foreign_keys": fetch(
            url,
            "SELECT table_name AS t, constraint_name AS name, "
            "referenced_table_name AS ref FROM information_schema.referential_constraints "
            "WHERE constraint_schema = DATABASE() ORDER BY table_name, constraint_name",
        ),
    }


def version_column_width(url: str) -> int:
    return fetch_scalar(
        url,
        "SELECT character_maximum_length FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = 'alembic_version' "
        "AND column_name = 'version_num'",
    )


def column_names(url: str, table: str) -> list[str]:
    rows = fetch(
        url,
        "SELECT column_name AS c FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = %s ORDER BY ordinal_position",
        (table,),
    )
    return [row["c"] for row in rows]


def index_names(url: str, table: str) -> set[str]:
    rows = fetch(
        url,
        "SELECT DISTINCT index_name AS i FROM information_schema.statistics "
        "WHERE table_schema = DATABASE() AND table_name = %s",
        (table,),
    )
    return {row["i"] for row in rows}


def table_exists(url: str, table: str) -> bool:
    return bool(
        fetch_scalar(
            url,
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = %s",
            (table,),
        )
    )


def set_versions(url: str, versions: list[str]) -> None:
    execute(
        url,
        "DELETE FROM alembic_version",
        *[("INSERT INTO alembic_version (version_num) VALUES (%s)", (v,)) for v in versions],
    )


class TestFreshDatabase:
    def test_strict_server_session_is_used(self, mysql_database):
        sql_mode = fetch_scalar(mysql_database, "SELECT @@SESSION.sql_mode")

        assert "STRICT_TRANS_TABLES" in sql_mode

    def test_upgrades_to_head_with_a_wide_version_table(self, mysql_database):
        upgrade(mysql_database)

        assert current_versions(mysql_database) == [HEAD]
        assert version_column_width(mysql_database) == 255

    def test_oversized_revisions_are_recorded_without_truncation(self, mysql_database):
        for revision in LONG_REVISIONS:
            upgrade(mysql_database, revision)

            assert current_versions(mysql_database) == [revision]
            assert len(revision) > 32

    def test_upgrade_to_head_after_the_long_revisions_keeps_the_chain_intact(self, mysql_database):
        upgrade(mysql_database, LONG_REVISIONS[0])
        upgrade(mysql_database)

        assert current_versions(mysql_database) == [HEAD]


class TestExistingVersionTable:
    def test_narrow_version_table_is_widened_before_the_first_long_revision(self, mysql_database):
        upgrade(mysql_database, "0001_initial")
        execute(mysql_database, "ALTER TABLE alembic_version MODIFY version_num VARCHAR(32) NOT NULL")
        assert version_column_width(mysql_database) == 32

        upgrade(mysql_database)

        assert current_versions(mysql_database) == [HEAD]
        assert version_column_width(mysql_database) == 255

    def test_narrow_version_table_at_head_is_widened_in_place(self, migrated_database):
        execute(migrated_database, "ALTER TABLE alembic_version MODIFY version_num VARCHAR(32) NOT NULL")

        upgrade(migrated_database)

        assert version_column_width(migrated_database) == 255
        assert current_versions(migrated_database) == [HEAD]

    @pytest.mark.parametrize("full_revision", LONG_REVISIONS)
    def test_truncated_revision_ids_are_restored(self, mysql_database, full_revision):
        """旧的 VARCHAR(32) 表在非 strict 模式下会把超长 ID 截断成 32 位。"""
        upgrade(mysql_database, full_revision)
        truncated = full_revision[:32]
        execute(
            mysql_database,
            ("UPDATE alembic_version SET version_num = %s", (truncated,)),
            "ALTER TABLE alembic_version MODIFY version_num VARCHAR(32) NOT NULL",
        )
        assert current_versions(mysql_database) == [truncated]

        upgrade(mysql_database)

        assert current_versions(mysql_database) == [HEAD]


class TestExplicitUrl:
    def test_explicit_attribute_wins_over_the_configured_database(self, mysql_database):
        """集成测试期间配置里的库地址是不可达的占位值，升级成功就说明用的是显式 URL。"""
        from core import config

        assert "invalid.invalid" in config.SQLALCHEMY_DATABASE_URI

        upgrade(mysql_database)

        assert current_versions(mysql_database) == [HEAD]

    def test_x_argument_wins_over_the_configured_database(self, mysql_database):
        cfg = Config(cmd_opts=Namespace(x=[f"db_url={mysql_database}"]))
        cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))

        command.upgrade(cfg, "head")

        assert current_versions(mysql_database) == [HEAD]


class TestReentrantMigrations:
    def test_replaying_any_revision_on_a_complete_schema_changes_nothing(self, migrated_database):
        """版本号停在任意 revision 之前、DDL 却已全部生效：重跑到 head 必须是空操作。"""
        execute(
            migrated_database,
            "UPDATE stake_reward_pool SET balance = 42 WHERE id = 1",
            "INSERT INTO `user` (id, name, coins_paid, user_plan) VALUES "
            "(1, 'paid', 10, 'paid'), (2, 'free', 0, 'free'), (3, 'custom', 5, 'admin')",
            "INSERT INTO ai_user_diary_pages (user_id, page_no, content) VALUES (1, 1, 'edited')",
        )
        expected_schema = schema_signature(migrated_database)
        expected_users = fetch(migrated_database, "SELECT id, user_plan FROM `user` ORDER BY id")
        script = ScriptDirectory.from_config(alembic_config(migrated_database))

        for revision in reversed(list(script.walk_revisions())):
            down = revision.down_revision
            parents = [] if down is None else ([down] if isinstance(down, str) else list(down))
            set_versions(migrated_database, parents)

            upgrade(migrated_database)

            assert current_versions(migrated_database) == [HEAD], revision.revision
            assert schema_signature(migrated_database) == expected_schema, revision.revision
            assert fetch(
                migrated_database, "SELECT id, user_plan FROM `user` ORDER BY id"
            ) == expected_users, revision.revision
            assert fetch_scalar(
                migrated_database, "SELECT balance FROM stake_reward_pool WHERE id = 1"
            ) == 42, revision.revision
            assert fetch_scalar(
                migrated_database,
                "SELECT content FROM ai_user_diary_pages WHERE user_id = 1 AND page_no = 1",
            ) == "edited", revision.revision

    def test_partially_applied_columns_are_completed(self, migrated_database):
        """0013 一次加三列；只剩一部分列时重跑能补齐，并保持列顺序。"""
        expected = column_names(migrated_database, "ai_schedules")
        execute(
            migrated_database,
            "ALTER TABLE ai_schedules DROP COLUMN last_run_at, DROP COLUMN recurrence_interval",
        )
        set_versions(migrated_database, ["0012_add_user_plan"])

        upgrade(migrated_database)

        assert column_names(migrated_database, "ai_schedules") == expected
        assert current_versions(migrated_database) == [HEAD]


class FailOnce:
    """让被替换的助手第一次满足条件时抛出，模拟迁移中途进程崩溃。"""

    def __init__(self, original, should_fail):
        self.original = original
        self.should_fail = should_fail
        self.failed = False

    def __call__(self, *args, **kwargs):
        if not self.failed and self.should_fail(*args, **kwargs):
            self.failed = True
            raise RuntimeError("simulated crash after the previous DDL was committed")
        return self.original(*args, **kwargs)


class TestPartialFailureRecovery:
    def test_crash_after_chat_records_id_ddl_is_recoverable(self, mysql_database, monkeypatch):
        from modules.core import migration_support as ms

        upgrade(mysql_database, REV_0016)
        seed_duplicate_rows(mysql_database)
        crash = FailOnce(ms.add_unique_key_if_missing, lambda table, *a, **k: table == "chat_records")
        monkeypatch.setattr(ms, "add_unique_key_if_missing", crash)

        with pytest.raises(RuntimeError, match="simulated crash"):
            upgrade(mysql_database)

        # DDL 已经生效，但版本号没有前进；去重的 DML 随事务回滚。
        assert crash.failed
        assert current_versions(mysql_database) == [REV_0016]
        extra = fetch_scalar(
            mysql_database,
            "SELECT extra FROM information_schema.columns WHERE table_schema = DATABASE() "
            "AND table_name = 'chat_records' AND column_name = 'id'",
        )
        assert "auto_increment" in extra.lower()
        assert fetch_scalar(
            mysql_database, "SELECT COUNT(*) FROM chat_records WHERE conversation_id = 1"
        ) == 3
        assert fetch_scalar(mysql_database, "SELECT COUNT(*) FROM chat_records_dedup_0017") == 0

        upgrade(mysql_database)

        assert current_versions(mysql_database) == [HEAD]
        survivors = fetch(mysql_database, "SELECT conversation_id, messages FROM chat_records ORDER BY 1")
        assert [row["conversation_id"] for row in survivors] == [1, 2]
        assert first_message_content(survivors[0]["messages"]) == "rotated"
        assert fetch_scalar(mysql_database, "SELECT COUNT(*) FROM chat_records_dedup_0017") == 2
        assert "uq_chat_records_conversation_id" in index_names(mysql_database, "chat_records")

    def test_crash_with_the_temporary_lottery_column_in_place_is_recoverable(
        self, mysql_database, monkeypatch
    ):
        from modules.core import migration_support as ms

        upgrade(mysql_database, REV_0016)
        seed_duplicate_rows(mysql_database)
        crash = FailOnce(ms.primary_key_columns, lambda table, *a, **k: table == "user_lottery")
        monkeypatch.setattr(ms, "primary_key_columns", crash)

        with pytest.raises(RuntimeError, match="simulated crash"):
            upgrade(mysql_database)

        assert current_versions(mysql_database) == [REV_0016]
        assert "_dedup_row_id_0017" in column_names(mysql_database, "user_lottery")

        upgrade(mysql_database)

        assert current_versions(mysql_database) == [HEAD]
        assert column_names(mysql_database, "user_lottery") == ["user_id", "last_lottery_date"]
        rows = fetch(mysql_database, "SELECT user_id, last_lottery_date FROM user_lottery ORDER BY 1")
        assert [row["user_id"] for row in rows] == [7, 8, 9]
        assert str(rows[0]["last_lottery_date"]) == "2024-03-01 00:00:00"
        assert fetch_scalar(mysql_database, "SELECT COUNT(*) FROM user_lottery_dedup_0017") == 2

    def test_crash_after_ddl_with_unrecorded_revision_is_recoverable_for_old_revisions(
        self, mysql_database
    ):
        """0007 的列已经加上，但版本号还停在 0006：重跑不能因为列已存在而失败。"""
        upgrade(mysql_database, "0006_drop_ai_user_diary")
        execute(
            mysql_database,
            "ALTER TABLE `user` ADD COLUMN `permanent_records_limit` INT NOT NULL DEFAULT 100",
        )

        upgrade(mysql_database)

        assert current_versions(mysql_database) == [HEAD]
        assert "permanent_records_limit" in column_names(mysql_database, "user")


class TestExistingSchemaCustomisations:
    def test_manually_fixed_constraints_are_left_alone(self, mysql_database):
        upgrade(mysql_database, REV_0016)
        execute(
            mysql_database,
            "ALTER TABLE chat_records MODIFY id INT NOT NULL AUTO_INCREMENT, ADD PRIMARY KEY (id), "
            "ADD UNIQUE KEY my_conversation (conversation_id)",
            "ALTER TABLE user_lottery ADD UNIQUE KEY my_user (user_id)",
        )

        upgrade(mysql_database)

        assert index_names(mysql_database, "chat_records") == {"PRIMARY", "my_conversation"}
        assert index_names(mysql_database, "user_lottery") == {"my_user"}
        assert not table_exists(mysql_database, "chat_records_dedup_0017")
        assert not table_exists(mysql_database, "user_lottery_dedup_0017")

    def test_existing_primary_key_without_auto_increment_is_made_auto_increment(self, mysql_database):
        upgrade(mysql_database, REV_0016)
        execute(mysql_database, "ALTER TABLE chat_records ADD PRIMARY KEY (id)")

        upgrade(mysql_database)

        row = fetch(
            mysql_database,
            "SELECT column_type AS ty, extra AS extra FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = 'chat_records' AND column_name = 'id'",
        )[0]
        assert "auto_increment" in row["extra"].lower()
        assert row["ty"] == "int"
        assert index_names(mysql_database, "chat_records") == {
            "PRIMARY",
            "uq_chat_records_conversation_id",
        }


class TestDowngrade:
    def test_schema_contracts_roundtrip(self, migrated_database):
        before = schema_signature(migrated_database)

        downgrade(migrated_database, REV_0016)

        assert current_versions(migrated_database) == [REV_0016]
        assert index_names(migrated_database, "chat_records") == set()
        assert index_names(migrated_database, "user_lottery") == set()

        upgrade(migrated_database)

        assert current_versions(migrated_database) == [HEAD]
        assert schema_signature(migrated_database) == before
