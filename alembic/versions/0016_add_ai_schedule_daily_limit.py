"""Persist each user's daily scheduled-message trigger count."""

from alembic import op

revision = "0016_add_ai_schedule_daily_limit"
down_revision = "0015_add_ai_idle_followups"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE `user` "
        "ADD COLUMN `ai_schedule_trigger_date` DATE NULL DEFAULT NULL "
        "AFTER `user_plan`, "
        "ADD COLUMN `ai_schedule_trigger_count` TINYINT UNSIGNED NOT NULL DEFAULT 0 "
        "AFTER `ai_schedule_trigger_date`"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE `user` "
        "DROP COLUMN `ai_schedule_trigger_count`, "
        "DROP COLUMN `ai_schedule_trigger_date`"
    )
