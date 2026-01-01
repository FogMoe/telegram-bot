"""Add ai_schedules table."""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0004_add_ai_schedules"
down_revision = "0003_add_ai_user_diary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE TABLE IF NOT EXISTS `ai_schedules` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `user_id` BIGINT NOT NULL,
  `run_at` DATETIME NOT NULL,
  `trigger_reason` VARCHAR(200) NOT NULL,
  `context` TEXT NULL,
  `prompt` TEXT NOT NULL,
  `status` ENUM('pending','executing','executed','cancelled','failed') NOT NULL DEFAULT 'pending',
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `executed_at` TIMESTAMP NULL DEFAULT NULL,
  `error` TEXT NULL,
  PRIMARY KEY (`id`),
  INDEX `idx_ai_schedules_user_status` (`user_id`, `status`),
  INDEX `idx_ai_schedules_run` (`status`, `run_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci"""
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS `ai_schedules`")
