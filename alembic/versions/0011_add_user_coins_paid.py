"""为用户表增加付费金币字段。"""

from alembic import op

revision = "0011_add_user_coins_paid"
down_revision = "0010_add_user_give_daily"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE `user` "
        "ADD COLUMN `coins_paid` INT NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE `user` DROP COLUMN `coins_paid`")
