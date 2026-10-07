"""Paid AI spam check for groups (/spam ai).

- group_spam_ai：每个付过费的群一行。`paid_until` 是有效期，每次付费在 `max(现在, paid_until)` 上加 30 天；
  `enabled` 为 0 表示管理员暂停了检查，有效期照常流逝。`reminded_soon_for`、`reminded_expired_for`
  记下已经为哪一个 `paid_until` 发过到期提醒，续费改变 `paid_until` 之后会重新提醒。
  扣款记在 coin_ledger（reason `spam_ai`，ref `chat:<chat_id>`），这里不另存付款记录。
- group_spam_ai_members：群成员已经被 AI 检查并判定为正常的消息条数，满额之后不再检查这个人。

新表，`CREATE TABLE IF NOT EXISTS` 即可重入。时间列由代码用数据库时钟（`UTC_TIMESTAMP(6)`）写入。
降级会丢弃付费记录，仅用于开发环境。
"""

from alembic import op

revision = "0023_group_spam_ai"
down_revision = "0022_group_x_feeds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE TABLE IF NOT EXISTS `group_spam_ai` (
  `chat_id` BIGINT NOT NULL,
  `enabled` TINYINT(1) NOT NULL DEFAULT 1,
  `paid_until` DATETIME(6) NOT NULL,
  `last_paid_by` BIGINT NOT NULL,
  `reminded_soon_for` DATETIME(6) NULL DEFAULT NULL,
  `reminded_expired_for` DATETIME(6) NULL DEFAULT NULL,
  `updated_at` DATETIME(6) NOT NULL,
  PRIMARY KEY (`chat_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci"""
    )
    op.execute(
        """CREATE TABLE IF NOT EXISTS `group_spam_ai_members` (
  `chat_id` BIGINT NOT NULL,
  `user_id` BIGINT NOT NULL,
  `checked_count` INT NOT NULL DEFAULT 0,
  `updated_at` DATETIME(6) NOT NULL,
  PRIMARY KEY (`chat_id`, `user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci"""
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS `group_spam_ai_members`")
    op.execute("DROP TABLE IF EXISTS `group_spam_ai`")
