"""Add index metadata to diary pages."""

from alembic import op

revision = "0014_add_ai_user_diary_page_index"
down_revision = "0013_add_ai_schedule_recurrence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE `ai_user_diary_pages` "
        "ADD COLUMN `title` VARCHAR(60) NULL DEFAULT NULL AFTER `page_no`, "
        "ADD COLUMN `summary` VARCHAR(120) NULL DEFAULT NULL AFTER `title`"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE `ai_user_diary_pages` "
        "DROP COLUMN `summary`, "
        "DROP COLUMN `title`"
    )
