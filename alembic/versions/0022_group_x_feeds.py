"""Per-group X (Twitter) account sync.

- group_x_feeds：每个群一行，行存在即表示这个群已经付过开通费（`paid_by`、`paid_op_key` 指向那笔扣款）。
  `x_handle` 是绑定的 X 账号，`last_seen_id` 是它已经同步到的最新帖子 id，轮询只发送比它新的帖子。
  解绑只把 `enabled` 置 0，账号与进度都保留：之后重新绑定不再收费，绑回同一个账号时从原来的进度继续。

新表，`CREATE TABLE IF NOT EXISTS` 即可重入。时间列由代码用数据库时钟（`UTC_TIMESTAMP(6)`）写入。
降级会丢弃开通记录，仅用于开发环境。
"""

from alembic import op

revision = "0022_group_x_feeds"
down_revision = "0021_game_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE TABLE IF NOT EXISTS `group_x_feeds` (
  `chat_id` BIGINT NOT NULL,
  `x_handle` VARCHAR(15) NOT NULL,
  `enabled` TINYINT(1) NOT NULL DEFAULT 1,
  `last_seen_id` BIGINT NOT NULL DEFAULT 0,
  `bound_by` BIGINT NULL DEFAULT NULL,
  `paid_by` BIGINT NOT NULL,
  `paid_op_key` VARCHAR(160) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `paid_at` DATETIME(6) NOT NULL,
  `updated_at` DATETIME(6) NOT NULL,
  PRIMARY KEY (`chat_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci"""
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS `group_x_feeds`")
