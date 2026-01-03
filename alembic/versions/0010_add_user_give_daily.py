"""Add daily give limit tracking table."""

from alembic import op

revision = "0010_add_user_give_daily"
down_revision = "0009_add_recharge_blocked_until"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE `user_give_daily` (
  `user_id` BIGINT NOT NULL,
  `give_date` DATE NOT NULL,
  `give_count` INT NOT NULL DEFAULT 0,
  PRIMARY KEY (`user_id`, `give_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS `user_give_daily`")
