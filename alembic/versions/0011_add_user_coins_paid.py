"""为用户表增加付费金币字段。"""

from alembic import op

from fogmoe_telegram_bot.core.migration_support import add_columns_if_missing

revision = "0011_add_user_coins_paid"
down_revision = "0010_add_user_give_daily"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_columns_if_missing(
        "user",
        [("coins_paid", "INT NOT NULL DEFAULT 0")],
    )


def downgrade() -> None:
    op.execute("ALTER TABLE `user` DROP COLUMN `coins_paid`")
