"""0019_coin_ledger：账本、奖池账本与充值请求表的 schema 契约，以及迁移的可重入性。"""

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
from sqlalchemy.exc import DataError, IntegrityError

REV_0018 = "0018_privacy_retention"
HEAD = head_revision()

LEDGER_INSERT = (
    "INSERT INTO coin_ledger (op_key, user_id, kind, delta_free, delta_paid, "
    "balance_free, balance_paid, reason) VALUES (%s, 1, 'credit', 1, 0, 1, 0, 'test')"
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


def unique_columns(url, table):
    rows = fetch(
        url,
        "SELECT index_name AS idx, column_name AS col FROM information_schema.statistics "
        "WHERE table_schema = DATABASE() AND table_name = %s AND non_unique = 0 "
        "ORDER BY index_name, seq_in_index",
        (table,),
    )
    result = {}
    for row in rows:
        result.setdefault(row["idx"], []).append(row["col"])
    return result


class TestLedgerTables:
    def test_op_key_is_unique_in_the_coin_ledger(self, migrated_database):
        assert unique_columns(migrated_database, "coin_ledger")["uq_coin_ledger_op_key"] == [
            "op_key"
        ]
        execute(migrated_database, (LEDGER_INSERT, ("k:1",)))

        with pytest.raises(IntegrityError):
            execute(migrated_database, (LEDGER_INSERT, ("k:1",)))

    def test_op_keys_are_case_sensitive(self, migrated_database):
        execute(migrated_database, (LEDGER_INSERT, ("Key:1",)))
        execute(migrated_database, (LEDGER_INSERT, ("key:1",)))

        assert fetch_scalar(migrated_database, "SELECT COUNT(*) FROM coin_ledger") == 2

    def test_op_key_is_unique_in_the_pool_ledger(self, migrated_database):
        insert = (
            "INSERT INTO stake_pool_ledger (op_key, kind, delta, balance_after, reason) "
            "VALUES (%s, 'credit', 1.00, 1.00, 'test')"
        )
        execute(migrated_database, (insert, ("p:1",)))

        with pytest.raises(IntegrityError):
            execute(migrated_database, (insert, ("p:1",)))

    def test_ledger_kind_is_restricted_in_strict_mode(self, migrated_database):
        with pytest.raises(DataError):
            execute(
                migrated_database,
                (
                    "INSERT INTO coin_ledger (op_key, user_id, kind, delta_free, delta_paid, "
                    "balance_free, balance_paid, reason) "
                    "VALUES ('k:bad', 1, 'gift', 1, 0, 1, 0, 'test')",
                    (),
                ),
            )

    def test_topup_requests_start_pending_and_only_known_statuses_are_allowed(
        self, migrated_database
    ):
        execute(
            migrated_database,
            "INSERT INTO topup_requests (user_id, coins, price_cents) VALUES (1, 50, 199)",
        )

        assert fetch_scalar(migrated_database, "SELECT status FROM topup_requests") == "pending"
        with pytest.raises(DataError):
            execute(
                migrated_database,
                "UPDATE topup_requests SET status = 'refunded' WHERE id = 1",
            )


class TestMigration:
    def test_replaying_the_revision_keeps_the_rows(self, migrated_database):
        execute(migrated_database, (LEDGER_INSERT, ("k:1",)))
        execute(
            migrated_database,
            "INSERT INTO topup_requests (user_id, coins, price_cents, status) "
            "VALUES (1, 50, 199, 'approved')",
        )
        execute(
            migrated_database,
            ("UPDATE alembic_version SET version_num = %s", (REV_0018,)),
        )

        upgrade(migrated_database)

        assert current_versions(migrated_database) == [HEAD]
        assert fetch_scalar(migrated_database, "SELECT COUNT(*) FROM coin_ledger") == 1
        assert fetch_scalar(migrated_database, "SELECT status FROM topup_requests") == "approved"

    def test_downgrade_drops_the_tables_and_upgrade_recreates_them(self, migrated_database):
        downgrade(migrated_database, REV_0018)

        assert current_versions(migrated_database) == [REV_0018]
        for table in ("coin_ledger", "stake_pool_ledger", "topup_requests"):
            assert not table_exists(migrated_database, table)

        upgrade(migrated_database)

        for table in ("coin_ledger", "stake_pool_ledger", "topup_requests"):
            assert table_exists(migrated_database, table)
