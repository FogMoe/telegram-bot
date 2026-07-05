"""Add recurrence fields to ai_schedules."""

from alembic import op

revision = "0013_add_ai_schedule_recurrence"
down_revision = "0012_add_user_plan"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE `ai_schedules` "
        "ADD COLUMN `recurrence_unit` ENUM('none','minute','hour','day') "
        "NOT NULL DEFAULT 'none' AFTER `run_at`, "
        "ADD COLUMN `recurrence_interval` INT NOT NULL DEFAULT 1 "
        "AFTER `recurrence_unit`, "
        "ADD COLUMN `last_run_at` DATETIME NULL DEFAULT NULL AFTER `executed_at`"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE `ai_schedules` "
        "DROP COLUMN `last_run_at`, "
        "DROP COLUMN `recurrence_interval`, "
        "DROP COLUMN `recurrence_unit`"
    )
