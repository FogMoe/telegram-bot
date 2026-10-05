"""为用户表增加套餐字段。"""

from alembic import op

from fogmoe_telegram_bot.core.migration_support import add_columns_if_missing

revision = "0012_add_user_plan"
down_revision = "0011_add_user_coins_paid"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from fogmoe_telegram_bot.core import config

    add_columns_if_missing(
        "user",
        [("user_plan", "VARCHAR(10) NOT NULL DEFAULT 'free'")],
    )
    # 回填只处理仍是默认值的行：首次运行结果不变，重跑不会覆盖之后的套餐变化。
    op.execute("UPDATE user SET user_plan = 'paid' WHERE coins_paid > 0 AND user_plan = 'free'")
    op.execute(
        "UPDATE user SET user_plan = 'admin' WHERE id = %d AND user_plan <> 'admin'"
        % int(config.ADMIN_USER_ID)
    )


def downgrade() -> None:
    op.execute("ALTER TABLE `user` DROP COLUMN `user_plan`")
