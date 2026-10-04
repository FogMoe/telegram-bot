"""0020_job_claims：claim 所有权列、尝试记录表，以及旧版本留下的 executing 行。"""

from mysql_support import (
    bind_app_engine,
    current_versions,
    downgrade,
    execute,
    fetch,
    fetch_scalar,
    head_revision,
    run,
    upgrade,
)

REV_0018 = "0018_privacy_retention"
HEAD = head_revision()


def columns(url: str, table: str) -> list[str]:
    return [
        row["c"]
        for row in fetch(
            url,
            "SELECT column_name AS c FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = %s ORDER BY ordinal_position",
            (table,),
        )
    ]


def table_exists(url: str, table: str) -> bool:
    return bool(
        fetch_scalar(
            url,
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = %s",
            (table,),
        )
    )


def seed_legacy_rows(url: str) -> None:
    """0018 时代的数据：一个旧版本进程崩溃后卡住的 executing 任务，和正常的行。"""
    execute(
        url,
        "INSERT INTO ai_schedules (id, user_id, run_at, trigger_reason, prompt, status) VALUES "
        "(1, 10, UTC_TIMESTAMP(), 'stuck', 'p', 'executing'), "
        "(2, 10, UTC_TIMESTAMP(), 'waiting', 'p', 'pending'), "
        "(3, 10, UTC_TIMESTAMP(), 'done', 'p', 'executed')",
        "INSERT INTO ai_idle_followups (user_id, last_activity_at, last_turn_at, next_run_at, "
        "typical_interval_seconds, recent_intervals, status, claim_until) VALUES "
        "(20, UTC_TIMESTAMP(), UTC_TIMESTAMP(), UTC_TIMESTAMP(), 600, '[]', 'executing', "
        "UTC_TIMESTAMP() - INTERVAL 1 HOUR), "
        "(21, UTC_TIMESTAMP(), UTC_TIMESTAMP(), UTC_TIMESTAMP(), 600, '[]', 'armed', NULL)",
    )


def test_upgrade_adds_claim_columns_and_the_attempt_table(mysql_database):
    upgrade(mysql_database, REV_0018)
    assert not table_exists(mysql_database, "ai_job_attempts")

    upgrade(mysql_database)

    assert current_versions(mysql_database) == [HEAD]
    for table in ("ai_schedules", "ai_idle_followups"):
        present = columns(mysql_database, table)
        for column in ("claim_token", "claim_attempts", "stage"):
            assert column in present, (table, column)
    assert "claim_until" in columns(mysql_database, "ai_schedules")
    assert columns(mysql_database, "ai_job_attempts") == [
        "id",
        "job_type",
        "job_id",
        "job_version",
        "claim_token",
        "attempt_no",
        "stage",
        "outcome",
        "daily_trigger_reserved",
        "error",
        "claimed_at",
        "stage_at",
        "finished_at",
    ]
    unique_tokens = fetch_scalar(
        mysql_database,
        "SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema = DATABASE() "
        "AND table_name = 'ai_job_attempts' AND index_name = 'uq_ai_job_attempts_token' "
        "AND non_unique = 0",
    )
    assert unique_tokens == 1


def test_rows_left_executing_by_the_old_version_become_expired_interrupted_claims(
    mysql_database,
):
    upgrade(mysql_database, REV_0018)
    seed_legacy_rows(mysql_database)

    upgrade(mysql_database)

    rows = {
        row["id"]: row
        for row in fetch(
            mysql_database,
            "SELECT id, status, stage, claim_token, claim_attempts, "
            "claim_until <= UTC_TIMESTAMP() AS expired FROM ai_schedules",
        )
    }
    assert rows[1]["stage"] == "generating" and rows[1]["expired"] == 1
    assert rows[1]["claim_token"] is None
    assert rows[2]["stage"] == "idle" and rows[3]["stage"] == "idle"
    idle = {
        row["user_id"]: row
        for row in fetch(mysql_database, "SELECT user_id, stage FROM ai_idle_followups")
    }
    assert idle[20]["stage"] == "generating" and idle[21]["stage"] == "idle"


def test_the_first_poll_closes_legacy_executing_rows_as_unknown_without_rerunning(
    mysql_database,
):
    from features.ai import idle_followup, scheduler

    upgrade(mysql_database, REV_0018)
    seed_legacy_rows(mysql_database)
    upgrade(mysql_database)

    with bind_app_engine(mysql_database):
        assert run(scheduler._recover_expired_schedules()) == 1
        assert run(idle_followup._recover_expired_followups()) == 1

    stuck = fetch(mysql_database, "SELECT status, error FROM ai_schedules WHERE id = 1")[0]
    assert stuck["status"] == "failed"
    assert "Interrupted during generating" in stuck["error"]
    assert fetch_scalar(mysql_database, "SELECT status FROM ai_schedules WHERE id = 2") == "pending"
    idle = fetch(mysql_database, "SELECT status, last_error FROM ai_idle_followups WHERE user_id = 20")[0]
    assert idle["status"] == "fired" and "outcome is unknown" in idle["last_error"]
    assert fetch_scalar(mysql_database, "SELECT status FROM ai_idle_followups WHERE user_id = 21") == "armed"


def test_replaying_the_migration_keeps_existing_claims(migrated_database):
    execute(
        migrated_database,
        (
            "INSERT INTO ai_schedules (id, user_id, run_at, trigger_reason, prompt, status, "
            "claim_token, claim_until, stage, claim_attempts) VALUES "
            "(1, 10, UTC_TIMESTAMP(), 'live', 'p', 'executing', %s, "
            "UTC_TIMESTAMP() + INTERVAL 5 MINUTE, 'claimed', 1)",
            ("a" * 32,),
        ),
    )
    before = fetch(migrated_database, "SELECT * FROM ai_schedules")

    execute(migrated_database, "DELETE FROM alembic_version")
    execute(
        migrated_database,
        ("INSERT INTO alembic_version (version_num) VALUES (%s)", (REV_0018,)),
    )
    upgrade(migrated_database)

    assert current_versions(migrated_database) == [HEAD]
    assert fetch(migrated_database, "SELECT * FROM ai_schedules") == before


def test_downgrade_removes_the_claim_objects_and_upgrade_restores_them(migrated_database):
    expected = {
        table: columns(migrated_database, table)
        for table in ("ai_schedules", "ai_idle_followups", "ai_job_attempts")
    }

    downgrade(migrated_database, REV_0018)

    assert current_versions(migrated_database) == [REV_0018]
    assert not table_exists(migrated_database, "ai_job_attempts")
    for table in ("ai_schedules", "ai_idle_followups"):
        assert "claim_token" not in columns(migrated_database, table)
        assert "stage" not in columns(migrated_database, table)

    upgrade(migrated_database)

    assert {
        table: columns(migrated_database, table)
        for table in ("ai_schedules", "ai_idle_followups", "ai_job_attempts")
    } == expected
