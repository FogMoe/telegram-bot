"""Add last_rotated_at to chat_records."""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0002_add_chat_records_last_rotated_at"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE `chat_records` "
        "ADD COLUMN `last_rotated_at` TIMESTAMP NULL DEFAULT NULL"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE `chat_records` DROP COLUMN `last_rotated_at`")
