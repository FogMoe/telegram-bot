"""Add recharge block column to user table."""

from alembic import op

from modules.core.migration_support import add_columns_if_missing

revision = "0009_add_recharge_blocked_until"
down_revision = "0008_merge_heads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_columns_if_missing(
        "user",
        [("recharge_blocked_until", "DATETIME NULL")],
    )


def downgrade() -> None:
    op.execute("ALTER TABLE `user` DROP COLUMN `recharge_blocked_until`")
