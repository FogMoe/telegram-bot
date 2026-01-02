"""新增频道绑定与频道消息表。"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0007_add_channel_bindings"
down_revision = "0006_drop_ai_user_diary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE TABLE IF NOT EXISTS `ai_user_channel_bindings` (
  `user_id` BIGINT NOT NULL,
  `channel_id` BIGINT NOT NULL,
  `channel_title` VARCHAR(255) NULL,
  `channel_username` VARCHAR(255) NULL,
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`user_id`),
  UNIQUE KEY `uniq_ai_user_channel_bindings_channel` (`channel_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci"""
    )
    op.execute(
        """CREATE TABLE IF NOT EXISTS `ai_channel_posts` (
  `channel_id` BIGINT NOT NULL,
  `message_id` BIGINT NOT NULL,
  `message_date` DATETIME NOT NULL,
  `message_type` VARCHAR(50) NOT NULL,
  `text` TEXT NULL,
  `caption` TEXT NULL,
  `file_id` VARCHAR(255) NULL,
  `raw_json` LONGTEXT NULL,
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`channel_id`, `message_id`),
  INDEX `idx_ai_channel_posts_channel_date` (`channel_id`, `message_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci"""
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS `ai_channel_posts`")
    op.execute("DROP TABLE IF EXISTS `ai_user_channel_bindings`")
