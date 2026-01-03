"""Merge multiple Alembic heads."""

from alembic import op

revision = "0008_merge_heads"
down_revision = ("0007_add_user_permanent_records_limit", "0002_stake_reward_pool")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
