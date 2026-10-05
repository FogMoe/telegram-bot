"""Repair schema contracts that the application code already relies on.

- chat_records：`id` 变成自增主键（原来是没有默认值的 INT NOT NULL，strict 模式下
  `INSERT` 不带 id 会失败），`conversation_id` 加唯一约束。
- user_lottery：`user_id` 加主键（`ON DUPLICATE KEY UPDATE` 依赖它）。

加约束之前先处理重复数据，被淘汰的行搬进备份表而不是直接删除：

- `chat_records_dedup_0017`：同一 conversation_id 只保留「最新」的一行。
  排序依次是 last_rotated_at 最新（NULL 视为最旧）、timestamp 最新、id 最大。
  应用的每次更新都是 `UPDATE ... WHERE conversation_id = %s`，会同时写到所有重复行，
  所以重复行只可能在从未被更新过时内容不同，较晚创建的一行就是较新的状态。
- `user_lottery_dedup_0017`：同一 user_id 保留最大的 last_lottery_date（NULL 视为最旧）。

迁移前会检查实际 schema：已经存在恰好覆盖目标列的主键/唯一索引（手工修过的部署）就跳过对应步骤；
每一步都可重入，DDL 隐式提交后中途失败可直接重跑。备份表的处置见 docs/database-migrations.md。
"""

from alembic import op

from fogmoe_telegram_bot.core import migration_support as ms

revision = "0017_schema_contracts"
down_revision = "0016_add_ai_schedule_daily_limit"
branch_labels = None
depends_on = None

CHAT_RECORDS_BACKUP = "chat_records_dedup_0017"
CHAT_RECORDS_UNIQUE = "uq_chat_records_conversation_id"
USER_LOTTERY_BACKUP = "user_lottery_dedup_0017"
USER_LOTTERY_UNIQUE = "uq_user_lottery_user_id"
# 用户抽奖表没有任何可区分重复行的列，去重期间临时加一个代理列，结束时删除。
USER_LOTTERY_SURROGATE = "_dedup_row_id_0017"

# 保留行的选择规则；k 是同一 conversation_id 下的候选行。
_CHAT_RECORDS_SURVIVOR = (
    "SELECT k.`id` FROM `chat_records` k WHERE k.`conversation_id` = c.`conversation_id` "
    "ORDER BY k.`last_rotated_at` IS NULL, k.`last_rotated_at` DESC, "
    "k.`timestamp` DESC, k.`id` DESC LIMIT 1"
)
_USER_LOTTERY_SURVIVOR = (
    f"SELECT k.`{USER_LOTTERY_SURROGATE}` FROM `user_lottery` k "
    "WHERE k.`user_id` = c.`user_id` "
    "ORDER BY k.`last_lottery_date` IS NULL, k.`last_lottery_date` DESC, "
    f"k.`{USER_LOTTERY_SURROGATE}` DESC LIMIT 1"
)


def _ensure_chat_records_id() -> None:
    info = ms.column_info("chat_records", "id")
    pk = ms.primary_key_columns("chat_records")

    if info is not None and "auto_increment" in (info["extra"] or "").lower():
        return
    if info is not None and pk == ("id",):
        op.execute(
            f"ALTER TABLE `chat_records` MODIFY COLUMN `id` {info['column_type']} "
            "NOT NULL AUTO_INCREMENT"
        )
        return

    # 应用从不读取 chat_records.id；存量行在非 strict 模式下可能全是 0，
    # 直接重建这一列，由 MySQL 按行的物理顺序重新编号。
    # 表上已有别的主键（手工改过）时退而求其次用唯一键，避免出现第二个主键。
    drop = "DROP COLUMN `id`, " if info is not None or ms.is_offline() else ""
    key = "UNIQUE KEY" if pk else "PRIMARY KEY"
    op.execute(
        f"ALTER TABLE `chat_records` {drop}"
        f"ADD COLUMN `id` BIGINT NOT NULL AUTO_INCREMENT {key} FIRST"
    )


def _dedup_chat_records() -> None:
    op.execute(
        f"""CREATE TABLE IF NOT EXISTS `{CHAT_RECORDS_BACKUP}` (
  `id` BIGINT NOT NULL,
  `conversation_id` BIGINT NOT NULL,
  `messages` JSON NOT NULL,
  `timestamp` TIMESTAMP NULL DEFAULT NULL,
  `last_rotated_at` TIMESTAMP NULL DEFAULT NULL,
  `kept_id` BIGINT NULL,
  `backed_up_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY `idx_{CHAT_RECORDS_BACKUP}_conversation` (`conversation_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci"""
    )
    # 先备份被淘汰的行，再删除已备份的行；只处理确实有重复的 conversation_id。
    op.execute(
        f"""INSERT INTO `{CHAT_RECORDS_BACKUP}`
  (`id`, `conversation_id`, `messages`, `timestamp`, `last_rotated_at`, `kept_id`)
SELECT c.`id`, c.`conversation_id`, c.`messages`, c.`timestamp`, c.`last_rotated_at`,
       ({_CHAT_RECORDS_SURVIVOR})
FROM `chat_records` c
JOIN (
  SELECT `conversation_id` FROM `chat_records`
  GROUP BY `conversation_id` HAVING COUNT(*) > 1
) d ON d.`conversation_id` = c.`conversation_id`
WHERE c.`id` <> ({_CHAT_RECORDS_SURVIVOR})
  AND NOT EXISTS (
    SELECT 1 FROM `{CHAT_RECORDS_BACKUP}` b
    WHERE b.`id` = c.`id` AND b.`conversation_id` = c.`conversation_id`
  )"""
    )
    op.execute(
        f"""DELETE c FROM `chat_records` c
JOIN `{CHAT_RECORDS_BACKUP}` b
  ON b.`id` = c.`id` AND b.`conversation_id` = c.`conversation_id`"""
    )


def _ensure_user_lottery_key() -> None:
    if ms.has_unique_index_on("user_lottery", ["user_id"]):
        return

    op.execute(
        f"""CREATE TABLE IF NOT EXISTS `{USER_LOTTERY_BACKUP}` (
  `user_id` BIGINT NOT NULL,
  `last_lottery_date` DATETIME NULL DEFAULT NULL,
  `row_id` BIGINT NULL,
  `backed_up_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY `idx_{USER_LOTTERY_BACKUP}_user` (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci"""
    )

    has_surrogate = ms.column_exists("user_lottery", USER_LOTTERY_SURROGATE)
    if not has_surrogate:
        op.execute(
            f"ALTER TABLE `user_lottery` ADD COLUMN `{USER_LOTTERY_SURROGATE}` "
            "BIGINT NOT NULL AUTO_INCREMENT UNIQUE"
        )
    op.execute(
        f"""INSERT INTO `{USER_LOTTERY_BACKUP}` (`user_id`, `last_lottery_date`, `row_id`)
SELECT c.`user_id`, c.`last_lottery_date`, c.`{USER_LOTTERY_SURROGATE}`
FROM `user_lottery` c
JOIN (
  SELECT `user_id` FROM `user_lottery` GROUP BY `user_id` HAVING COUNT(*) > 1
) d ON d.`user_id` = c.`user_id`
WHERE c.`{USER_LOTTERY_SURROGATE}` <> ({_USER_LOTTERY_SURVIVOR})
  AND NOT EXISTS (
    SELECT 1 FROM `{USER_LOTTERY_BACKUP}` b
    WHERE b.`row_id` = c.`{USER_LOTTERY_SURROGATE}` AND b.`user_id` = c.`user_id`
  )"""
    )
    op.execute(
        f"""DELETE c FROM `user_lottery` c
JOIN `{USER_LOTTERY_BACKUP}` b
  ON b.`row_id` = c.`{USER_LOTTERY_SURROGATE}` AND b.`user_id` = c.`user_id`"""
    )

    # 表上已有别的主键（手工改过）时用唯一键，避免出现第二个主键。
    key = (
        f"ADD UNIQUE KEY `{USER_LOTTERY_UNIQUE}` (`user_id`)"
        if ms.primary_key_columns("user_lottery")
        else "ADD PRIMARY KEY (`user_id`)"
    )
    drop = f"DROP COLUMN `{USER_LOTTERY_SURROGATE}`, "
    op.execute(f"ALTER TABLE `user_lottery` {drop}{key}")


def upgrade() -> None:
    _ensure_chat_records_id()
    # 已经有覆盖 conversation_id 的主键/唯一索引（手工修过）就不会有重复，也不需要备份表。
    if not ms.has_unique_index_on("chat_records", ["conversation_id"]):
        _dedup_chat_records()
        ms.add_unique_key_if_missing("chat_records", CHAT_RECORDS_UNIQUE, ["conversation_id"])
    _ensure_user_lottery_key()


def downgrade() -> None:
    # 备份表保留不动：里面是去重时被淘汰的数据。
    ms.drop_index_if_exists("chat_records", CHAT_RECORDS_UNIQUE)
    if ms.is_offline() or ms.primary_key_columns("chat_records") == ("id",):
        op.execute("ALTER TABLE `chat_records` MODIFY COLUMN `id` INT NOT NULL, DROP PRIMARY KEY")

    if ms.is_offline() or ms.primary_key_columns("user_lottery") == ("user_id",):
        op.execute("ALTER TABLE `user_lottery` DROP PRIMARY KEY")
    else:
        ms.drop_index_if_exists("user_lottery", USER_LOTTERY_UNIQUE)
