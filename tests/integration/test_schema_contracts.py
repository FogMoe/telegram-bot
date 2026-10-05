"""schema 契约：应用代码依赖的约束必须由迁移真正建出来（strict 模式的 MySQL）。"""

import json
from datetime import datetime

import pytest
from legacy_data import first_message_content, seed_duplicate_rows
from mysql_support import (
    head_revision,
    bind_app_engine,
    current_versions,
    execute,
    fetch,
    fetch_scalar,
    run,
    upgrade,
)
from sqlalchemy.exc import IntegrityError

from fogmoe_telegram_bot.core import sql
from fogmoe_telegram_bot.features.economy.repositories import lottery as lottery_repository

REV_0016 = "0016_add_ai_schedule_daily_limit"
HEAD = head_revision()


async def save_lottery_date(user_id):
    """像抽奖那样写一次时间戳：每日抽奖靠 `user_lottery` 的唯一键做 upsert。"""
    async with sql.transaction() as connection:
        await lottery_repository.save_last_lottery_date(connection, user_id, datetime.now())


class TestFreshDatabase:
    def test_first_conversation_insert_succeeds(self, app_database):
        from fogmoe_telegram_bot.core import chat_records

        run(chat_records.insert_chat_record(1001, "user", "hello"))

        rows = fetch(app_database, "SELECT id, conversation_id, messages FROM chat_records")
        assert len(rows) == 1
        assert rows[0]["id"] >= 1
        assert rows[0]["conversation_id"] == 1001
        assert "hello" in rows[0]["messages"]

    def test_later_turns_extend_the_same_row(self, app_database):
        from fogmoe_telegram_bot.core import chat_records

        run(chat_records.insert_chat_record(1001, "user", "first"))
        run(chat_records.insert_chat_record(1001, "assistant", "second"))
        run(chat_records.insert_chat_record(1002, "user", "someone else"))

        counts = fetch(
            app_database,
            "SELECT conversation_id, COUNT(*) AS n FROM chat_records GROUP BY conversation_id",
        )
        assert {row["conversation_id"]: row["n"] for row in counts} == {1001: 1, 1002: 1}
        stored = fetch_scalar(
            app_database, "SELECT messages FROM chat_records WHERE conversation_id = 1001"
        )
        assert "first" in stored and "second" in stored

    def test_conversation_id_is_unique(self, app_database):
        insert = "INSERT INTO chat_records (conversation_id, messages) VALUES (5, %s)"
        execute(app_database, (insert, (json.dumps([]),)))

        with pytest.raises(IntegrityError):
            execute(app_database, (insert, (json.dumps([]),)))

    def test_repeated_daily_lottery_updates_keep_a_single_row(self, app_database):
        run(save_lottery_date(42))
        first = run(lottery_repository.get_last_lottery_date(42))
        execute(
            app_database,
            "UPDATE user_lottery SET last_lottery_date = '2000-01-01 00:00:00' WHERE user_id = 42",
        )
        run(save_lottery_date(42))

        assert fetch_scalar(app_database, "SELECT COUNT(*) FROM user_lottery WHERE user_id = 42") == 1
        latest = run(lottery_repository.get_last_lottery_date(42))
        assert latest >= first

    def test_lottery_user_id_is_unique(self, app_database):
        execute(app_database, "INSERT INTO user_lottery (user_id) VALUES (1)")

        with pytest.raises(IntegrityError):
            execute(app_database, "INSERT INTO user_lottery (user_id) VALUES (1)")


class TestLegacyDatabaseWithDuplicates:
    @pytest.fixture
    def upgraded_legacy_database(self, mysql_database):
        upgrade(mysql_database, REV_0016)
        seed_duplicate_rows(mysql_database)
        upgrade(mysql_database)
        return mysql_database

    def test_newest_conversation_row_is_kept(self, upgraded_legacy_database):
        rows = fetch(
            upgraded_legacy_database,
            "SELECT conversation_id, messages FROM chat_records ORDER BY conversation_id",
        )

        assert [row["conversation_id"] for row in rows] == [1, 2]
        assert first_message_content(rows[0]["messages"]) == "rotated"
        assert current_versions(upgraded_legacy_database) == [HEAD]

    def test_evicted_conversation_rows_are_moved_to_the_backup_table(self, upgraded_legacy_database):
        backup = fetch(
            upgraded_legacy_database,
            "SELECT conversation_id, messages, kept_id FROM chat_records_dedup_0017 ORDER BY id",
        )
        kept_id = fetch_scalar(
            upgraded_legacy_database, "SELECT id FROM chat_records WHERE conversation_id = 1"
        )

        assert [first_message_content(row["messages"]) for row in backup] == ["oldest", "newer"]
        assert {row["conversation_id"] for row in backup} == {1}
        assert {row["kept_id"] for row in backup} == {kept_id}

    def test_conversation_rows_get_distinct_ids_and_the_unique_constraint(self, upgraded_legacy_database):
        ids = [row["id"] for row in fetch(upgraded_legacy_database, "SELECT id FROM chat_records")]

        assert len(set(ids)) == len(ids)
        assert all(value > 0 for value in ids)
        with pytest.raises(IntegrityError):
            execute(
                upgraded_legacy_database,
                "INSERT INTO chat_records (conversation_id, messages) VALUES (1, '[]')",
            )

    def test_latest_lottery_row_is_kept_per_user(self, upgraded_legacy_database):
        rows = fetch(
            upgraded_legacy_database,
            "SELECT user_id, last_lottery_date FROM user_lottery ORDER BY user_id",
        )

        assert [row["user_id"] for row in rows] == [7, 8, 9]
        assert str(rows[0]["last_lottery_date"]) == "2024-03-01 00:00:00"

    def test_evicted_lottery_rows_are_moved_to_the_backup_table(self, upgraded_legacy_database):
        backup = fetch(
            upgraded_legacy_database,
            "SELECT user_id, last_lottery_date FROM user_lottery_dedup_0017 "
            "ORDER BY last_lottery_date IS NULL, last_lottery_date",
        )

        assert [(row["user_id"], str(row["last_lottery_date"])) for row in backup] == [
            (7, "2024-01-01 00:00:00"),
            (7, "None"),
        ]
        with pytest.raises(IntegrityError):
            execute(upgraded_legacy_database, "INSERT INTO user_lottery (user_id) VALUES (7)")

    def test_application_keeps_working_on_the_upgraded_database(self, upgraded_legacy_database):
        from fogmoe_telegram_bot.core import chat_records

        with bind_app_engine(upgraded_legacy_database):
            run(chat_records.insert_chat_record(1, "user", "after the upgrade"))
            run(save_lottery_date(7))

        assert fetch_scalar(
            upgraded_legacy_database, "SELECT COUNT(*) FROM chat_records WHERE conversation_id = 1"
        ) == 1
        stored = fetch_scalar(
            upgraded_legacy_database, "SELECT messages FROM chat_records WHERE conversation_id = 1"
        )
        assert "rotated" in stored and "after the upgrade" in stored
        assert fetch_scalar(
            upgraded_legacy_database, "SELECT COUNT(*) FROM user_lottery WHERE user_id = 7"
        ) == 1
