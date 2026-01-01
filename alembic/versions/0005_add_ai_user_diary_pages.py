"""Add ai_user_diary_pages table."""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0005_add_ai_user_diary_pages"
down_revision = "0004_add_ai_schedules"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE TABLE IF NOT EXISTS `ai_user_diary_pages` (
  `user_id` BIGINT NOT NULL,
  `page_no` INT NOT NULL,
  `content` TEXT NOT NULL,
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`user_id`, `page_no`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci"""
    )
    op.execute(
        """
        INSERT INTO ai_user_diary_pages (user_id, page_no, content, created_at, updated_at)
        SELECT user_id, 1, content, created_at, updated_at
        FROM ai_user_diary
        WHERE content IS NOT NULL AND content != ''
        ON DUPLICATE KEY UPDATE content = VALUES(content), updated_at = VALUES(updated_at)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS `ai_user_diary_pages`")
