"""Add permanent_records_limit to user table."""

from alembic import op

revision = "0007_add_user_permanent_records_limit"
down_revision = "0006_drop_ai_user_diary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE `user` "
        "ADD COLUMN `permanent_records_limit` INT NOT NULL DEFAULT 100"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE `user` DROP COLUMN `permanent_records_limit`")
