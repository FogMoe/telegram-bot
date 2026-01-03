"""为用户表增加套餐字段。"""

from alembic import op

from modules.core import config

revision = "0012_add_user_plan"
down_revision = "0011_add_user_coins_paid"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE `user` "
        "ADD COLUMN `user_plan` VARCHAR(10) NOT NULL DEFAULT 'free'"
    )
    op.execute("UPDATE user SET user_plan = 'paid' WHERE coins_paid > 0")
    op.execute(
        "UPDATE user SET user_plan = 'admin' WHERE id = %s"
        % config.ADMIN_USER_ID
    )


def downgrade() -> None:
    op.execute("ALTER TABLE `user` DROP COLUMN `user_plan`")
