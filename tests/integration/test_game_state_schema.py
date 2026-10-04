"""0021_game_state：下注轮次、下注与石头剪刀布对局表的 schema 契约，以及迁移的可重入性。"""

import pytest
from mysql_support import (
    current_versions,
    downgrade,
    execute,
    fetch,
    fetch_scalar,
    head_revision,
    upgrade,
)
from sqlalchemy.exc import IntegrityError

REV_0020 = "0020_job_claims"
HEAD = head_revision()
TABLES = ("gamble_rounds", "gamble_bets", "rps_games")

NEW_ROUND = (
    "INSERT INTO gamble_rounds (chat_id, created_at, closes_at, status, active_slot) "
    "VALUES (-1, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6) + INTERVAL 300 SECOND, %s, %s)"
)
NEW_BET = (
    "INSERT INTO gamble_bets (round_id, user_id, username, amount, op_key, created_at) "
    "VALUES (%s, %s, 'u', 5, %s, UTC_TIMESTAMP(6))"
)
NEW_GAME = (
    "INSERT INTO rps_games (p1_id, p1_name, p1_chat_id, p2_id, p2_name, p2_chat_id, "
    "created_at, expires_at) VALUES (1, 'a', 1, 2, 'b', 2, UTC_TIMESTAMP(6), "
    "UTC_TIMESTAMP(6) + INTERVAL 120 SECOND)"
)


def table_exists(url, table):
    return bool(
        fetch_scalar(
            url,
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = %s",
            (table,),
        )
    )


class TestSchema:
    def test_only_one_round_can_be_open_at_a_time(self, migrated_database):
        execute(migrated_database, (NEW_ROUND, ("open", 1)))

        with pytest.raises(IntegrityError):
            execute(migrated_database, (NEW_ROUND, ("open", 1)))

    def test_any_number_of_finished_rounds_can_coexist_with_an_open_one(self, migrated_database):
        execute(migrated_database, (NEW_ROUND, ("open", 1)))
        for _ in range(3):
            execute(migrated_database, (NEW_ROUND, ("settled", None)))

        assert fetch_scalar(migrated_database, "SELECT COUNT(*) FROM gamble_rounds") == 4

    def test_a_player_has_one_bet_per_round_and_op_keys_are_unique(self, migrated_database):
        execute(migrated_database, (NEW_ROUND, ("open", 1)))
        execute(migrated_database, (NEW_BET, (1, 7, "gamble:1:bet:7")))

        with pytest.raises(IntegrityError):
            execute(migrated_database, (NEW_BET, (1, 7, "gamble:1:bet:7-again")))
        with pytest.raises(IntegrityError):
            execute(migrated_database, (NEW_BET, (1, 8, "gamble:1:bet:7")))
        execute(migrated_database, (NEW_BET, (1, 8, "gamble:1:bet:8")))

        assert fetch_scalar(migrated_database, "SELECT COUNT(*) FROM gamble_bets") == 2

    def test_a_new_game_starts_in_the_choosing_state_without_choices(self, migrated_database):
        execute(migrated_database, NEW_GAME)

        row = fetch(migrated_database, "SELECT * FROM rps_games")[0]
        assert row["status"] == "choosing" and row["outcome"] is None
        assert row["p1_choice"] is None and row["p2_choice"] is None
        assert row["announced_at"] is None and row["finished_at"] is None


class TestMigration:
    def test_replaying_the_revision_keeps_the_rows(self, migrated_database):
        execute(migrated_database, (NEW_ROUND, ("open", 1)))
        execute(migrated_database, (NEW_BET, (1, 7, "gamble:1:bet:7")))
        execute(migrated_database, NEW_GAME)
        execute(migrated_database, ("UPDATE alembic_version SET version_num = %s", (REV_0020,)))

        upgrade(migrated_database)

        assert current_versions(migrated_database) == [HEAD]
        assert fetch_scalar(migrated_database, "SELECT COUNT(*) FROM gamble_rounds") == 1
        assert fetch_scalar(migrated_database, "SELECT COUNT(*) FROM gamble_bets") == 1
        assert fetch_scalar(migrated_database, "SELECT COUNT(*) FROM rps_games") == 1

    def test_a_partially_applied_revision_is_completed(self, migrated_database):
        execute(migrated_database, (NEW_ROUND, ("open", 1)))
        execute(migrated_database, "DROP TABLE gamble_bets")
        execute(migrated_database, ("UPDATE alembic_version SET version_num = %s", (REV_0020,)))

        upgrade(migrated_database)

        assert current_versions(migrated_database) == [HEAD]
        assert table_exists(migrated_database, "gamble_bets")
        assert fetch_scalar(migrated_database, "SELECT COUNT(*) FROM gamble_rounds") == 1

    def test_downgrade_drops_the_tables_and_upgrade_recreates_them(self, migrated_database):
        downgrade(migrated_database, REV_0020)

        assert current_versions(migrated_database) == [REV_0020]
        for table in TABLES:
            assert not table_exists(migrated_database, table)

        upgrade(migrated_database)

        for table in TABLES:
            assert table_exists(migrated_database, table)
