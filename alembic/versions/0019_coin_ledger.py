"""Add the coin ledger, the reward-pool ledger and durable top-up requests.

- coin_ledger：每次余额变动一行，`op_key` 唯一。重放同一个 op_key 时由唯一约束兜底，
  余额服务（core/balance.py）据此保证幂等。记录变动量与变动后的 free / paid 余额，
  对账方法见 docs/balance-service.md。不加外键：用户被删除后账本仍要保留。
- stake_pool_ledger：奖池的变动记录。奖池是 DECIMAL(20,2) 且没有归属用户，
  混进 coin_ledger 会让「按用户汇总 = 用户余额」的对账失效，所以单独一张表，
  同样以 `op_key` 唯一。
- topup_requests：管理员人工充值请求的持久身份。审批按钮只携带请求 id，
  `pending -> approved/rejected/blocked` 只允许发生一次。

三张表都是新表，`CREATE TABLE IF NOT EXISTS` 即可重入。降级会丢弃账本，仅用于开发环境。
"""

from alembic import op

revision = "0019_coin_ledger"
down_revision = "0018_privacy_retention"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # op_key 只允许可打印 ASCII（服务层校验），ascii_bin 让比较区分大小写且一个字符一个字节。
    op.execute(
        """CREATE TABLE IF NOT EXISTS `coin_ledger` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `op_key` VARCHAR(160) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `user_id` BIGINT NOT NULL,
  `kind` ENUM('credit','debit','refund') NOT NULL,
  `delta_free` INT NOT NULL,
  `delta_paid` INT NOT NULL,
  `balance_free` INT NOT NULL,
  `balance_paid` INT NOT NULL,
  `reason` VARCHAR(64) NOT NULL,
  `ref` VARCHAR(160) CHARACTER SET ascii COLLATE ascii_bin NULL DEFAULT NULL,
  `created_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_coin_ledger_op_key` (`op_key`),
  KEY `idx_coin_ledger_user_created` (`user_id`, `created_at`),
  KEY `idx_coin_ledger_ref` (`ref`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci"""
    )
    op.execute(
        """CREATE TABLE IF NOT EXISTS `stake_pool_ledger` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `op_key` VARCHAR(160) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `kind` ENUM('credit','debit') NOT NULL,
  `delta` DECIMAL(20,2) NOT NULL,
  `balance_after` DECIMAL(20,2) NOT NULL,
  `reason` VARCHAR(64) NOT NULL,
  `ref` VARCHAR(160) CHARACTER SET ascii COLLATE ascii_bin NULL DEFAULT NULL,
  `created_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_stake_pool_ledger_op_key` (`op_key`),
  KEY `idx_stake_pool_ledger_created` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci"""
    )
    op.execute(
        """CREATE TABLE IF NOT EXISTS `topup_requests` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `user_id` BIGINT NOT NULL,
  `coins` INT NOT NULL,
  `price_cents` INT NOT NULL,
  `status` ENUM('pending','approved','rejected','blocked') NOT NULL DEFAULT 'pending',
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `decided_at` DATETIME NULL DEFAULT NULL,
  `decided_by` BIGINT NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_topup_requests_user_created` (`user_id`, `created_at`),
  KEY `idx_topup_requests_status_created` (`status`, `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci"""
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS `topup_requests`")
    op.execute("DROP TABLE IF EXISTS `stake_pool_ledger`")
    op.execute("DROP TABLE IF EXISTS `coin_ledger`")
