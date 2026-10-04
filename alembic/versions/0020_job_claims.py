"""Give scheduled-task and idle-follow-up claims an owner, a lease and an attempt log.

- `ai_schedules` / `ai_idle_followups` 新增 `claim_token`（每次 claim 随机生成）、
  `claim_attempts`、`stage`；`ai_schedules` 另有 `claim_until`（`ai_idle_followups` 早已有）。
- 新表 `ai_job_attempts`：每次 claim 一行，记录阶段与结果。
- 旧版本留下的 `executing` 行（没有租约、没有 token）按「执行中途中断」处理：
  stage 记为 generating，租约立即到期，下一次轮询按结果未知收尾，不会重跑。

所有语句都可重入：建表用 IF NOT EXISTS，加列和加索引前先查 information_schema，
数据回填只命中还没有 token 的行。
"""

from alembic import op

from modules.core.migration_support import add_columns_if_missing, index_exists

revision = "0020_job_claims"
down_revision = "0019_coin_ledger"
branch_labels = None
depends_on = None


def _add_index_if_missing(table: str, name: str, columns: str) -> None:
    if not index_exists(table, name):
        op.execute(f"ALTER TABLE `{table}` ADD INDEX `{name}` ({columns})")


def upgrade() -> None:
    add_columns_if_missing(
        "ai_schedules",
        [
            ("claim_token", "CHAR(32) NULL DEFAULT NULL AFTER `error`"),
            ("claim_until", "DATETIME NULL DEFAULT NULL AFTER `claim_token`"),
            ("claim_attempts", "INT NOT NULL DEFAULT 0 AFTER `claim_until`"),
            ("stage", "VARCHAR(16) NOT NULL DEFAULT 'idle' AFTER `claim_attempts`"),
        ],
    )
    _add_index_if_missing("ai_schedules", "idx_ai_schedules_claim", "`status`, `claim_until`")

    add_columns_if_missing(
        "ai_idle_followups",
        [
            ("claim_token", "CHAR(32) NULL DEFAULT NULL AFTER `last_error`"),
            ("claim_attempts", "INT NOT NULL DEFAULT 0 AFTER `claim_token`"),
            ("stage", "VARCHAR(16) NOT NULL DEFAULT 'idle' AFTER `claim_attempts`"),
        ],
    )

    op.execute(
        """CREATE TABLE IF NOT EXISTS `ai_job_attempts` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `job_type` VARCHAR(16) NOT NULL,
  `job_id` BIGINT NOT NULL,
  `job_version` BIGINT NULL DEFAULT NULL,
  `claim_token` CHAR(32) NOT NULL,
  `attempt_no` INT NOT NULL DEFAULT 1,
  `stage` VARCHAR(16) NOT NULL DEFAULT 'claimed',
  `outcome` VARCHAR(16) NULL DEFAULT NULL,
  `daily_trigger_reserved` TINYINT(1) NOT NULL DEFAULT 0,
  `error` VARCHAR(500) NULL DEFAULT NULL,
  `claimed_at` DATETIME NOT NULL,
  `stage_at` DATETIME NOT NULL,
  `finished_at` DATETIME NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_ai_job_attempts_token` (`claim_token`),
  INDEX `idx_ai_job_attempts_job` (`job_type`, `job_id`, `id`),
  INDEX `idx_ai_job_attempts_open` (`outcome`, `stage_at`),
  INDEX `idx_ai_job_attempts_finished` (`finished_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci"""
    )

    # 旧版本留下的 executing 行：没有 token 也没有租约，已经无人认领。
    op.execute(
        "UPDATE `ai_schedules` "
        "SET `stage` = 'generating', `claim_until` = UTC_TIMESTAMP() "
        "WHERE `status` = 'executing' AND `claim_token` IS NULL AND `claim_until` IS NULL"
    )
    op.execute(
        "UPDATE `ai_idle_followups` "
        "SET `stage` = 'generating' "
        "WHERE `status` = 'executing' AND `claim_token` IS NULL AND `stage` = 'idle'"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS `ai_job_attempts`")
    op.execute(
        "ALTER TABLE `ai_idle_followups` "
        "DROP COLUMN `stage`, "
        "DROP COLUMN `claim_attempts`, "
        "DROP COLUMN `claim_token`"
    )
    op.execute("ALTER TABLE `ai_schedules` DROP INDEX `idx_ai_schedules_claim`")
    op.execute(
        "ALTER TABLE `ai_schedules` "
        "DROP COLUMN `stage`, "
        "DROP COLUMN `claim_attempts`, "
        "DROP COLUMN `claim_until`, "
        "DROP COLUMN `claim_token`"
    )
