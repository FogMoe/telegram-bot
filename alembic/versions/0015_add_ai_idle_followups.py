"""Add idle follow-up state for private AI conversations."""

from alembic import op

revision = "0015_add_ai_idle_followups"
down_revision = "0014_add_ai_user_diary_page_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE TABLE IF NOT EXISTS `ai_idle_followups` (
  `user_id` BIGINT NOT NULL,
  `last_activity_at` DATETIME NOT NULL,
  `last_turn_at` DATETIME NOT NULL,
  `next_run_at` DATETIME NOT NULL,
  `typical_interval_seconds` INT NOT NULL,
  `recent_intervals` JSON NOT NULL,
  `activity_version` BIGINT NOT NULL DEFAULT 1,
  `status` ENUM('armed','executing','fired') NOT NULL DEFAULT 'armed',
  `claim_until` DATETIME NULL DEFAULT NULL,
  `retry_count` INT NOT NULL DEFAULT 0,
  `last_fired_at` DATETIME NULL DEFAULT NULL,
  `last_error` VARCHAR(500) NULL DEFAULT NULL,
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`user_id`),
  INDEX `idx_ai_idle_followups_due` (`status`, `next_run_at`),
  INDEX `idx_ai_idle_followups_claim` (`status`, `claim_until`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci"""
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS `ai_idle_followups`")
