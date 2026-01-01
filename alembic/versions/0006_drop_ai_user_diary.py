"""Drop ai_user_diary table."""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0006_drop_ai_user_diary"
down_revision = "0005_add_ai_user_diary_pages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS `ai_user_diary`")


def downgrade() -> None:
    op.execute(
        """CREATE TABLE IF NOT EXISTS `ai_user_diary` (
  `user_id` BIGINT NOT NULL,
  `content` TEXT NOT NULL,
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci"""
    )
