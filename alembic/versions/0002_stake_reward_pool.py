"""Add stake reward pool table."""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0002_stake_reward_pool"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE IF NOT EXISTS `stake_reward_pool` (
  `id` TINYINT NOT NULL,
  `balance` DECIMAL(20,2) NOT NULL DEFAULT 0,
  `updated_at` DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
    # 重跑时保留已有的奖池余额，不要覆盖。
    op.execute(
        "INSERT INTO `stake_reward_pool` (`id`, `balance`) VALUES (1, 0) "
        "ON DUPLICATE KEY UPDATE `id` = `id`"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS `stake_reward_pool`")
