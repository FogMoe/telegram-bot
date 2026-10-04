"""Persist the money-bearing state of the multiplayer bet and rock-paper-scissors games.

- gamble_rounds / gamble_bets：多人下注的轮次与下注。轮次有持久 id、所在 chat/message、
  状态与截止时间；`active_slot` 在轮次开放期间为 1、终结时置 NULL，唯一键保证同一时间
  只有一个开放轮次。`(round_id, user_id)` 唯一，下注记录对应的 debit op_key。
- rps_games：石头剪刀布对局。两名玩家、双方的面板消息、选择、状态与结果、创建/过期时间。
  入场扣款与对局创建在同一个事务里提交，之后的退款与奖金都以对局 id 派生 op_key。

三张表都是新表，`CREATE TABLE IF NOT EXISTS` 即可重入。时间列一律由代码用数据库时钟
（`UTC_TIMESTAMP(6)`）写入，不设默认值。降级会丢弃对局记录，仅用于开发环境。
"""

from alembic import op

revision = "0021_game_state"
down_revision = "0020_job_claims"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE TABLE IF NOT EXISTS `gamble_rounds` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `chat_id` BIGINT NOT NULL,
  `message_id` BIGINT NULL DEFAULT NULL,
  `status` VARCHAR(16) NOT NULL DEFAULT 'open',
  `active_slot` TINYINT NULL DEFAULT 1,
  `created_at` DATETIME(6) NOT NULL,
  `closes_at` DATETIME(6) NOT NULL,
  `settled_at` DATETIME(6) NULL DEFAULT NULL,
  `winner_id` BIGINT NULL DEFAULT NULL,
  `prize` INT NOT NULL DEFAULT 0,
  `announced_at` DATETIME(6) NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_gamble_rounds_active_slot` (`active_slot`),
  KEY `idx_gamble_rounds_status_closes` (`status`, `closes_at`),
  KEY `idx_gamble_rounds_announce` (`announced_at`, `settled_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci"""
    )
    op.execute(
        """CREATE TABLE IF NOT EXISTS `gamble_bets` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `round_id` BIGINT NOT NULL,
  `user_id` BIGINT NOT NULL,
  `username` VARCHAR(255) NOT NULL,
  `amount` INT NOT NULL,
  `op_key` VARCHAR(160) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `created_at` DATETIME(6) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_gamble_bets_round_user` (`round_id`, `user_id`),
  UNIQUE KEY `uq_gamble_bets_op_key` (`op_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci"""
    )
    op.execute(
        """CREATE TABLE IF NOT EXISTS `rps_games` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `status` VARCHAR(16) NOT NULL DEFAULT 'choosing',
  `outcome` VARCHAR(16) NULL DEFAULT NULL,
  `same_chat` TINYINT(1) NOT NULL DEFAULT 0,
  `p1_id` BIGINT NOT NULL,
  `p1_name` VARCHAR(255) NOT NULL,
  `p1_chat_id` BIGINT NOT NULL,
  `p1_message_id` BIGINT NULL DEFAULT NULL,
  `p1_private_msg_id` BIGINT NULL DEFAULT NULL,
  `p1_choice` VARCHAR(8) NULL DEFAULT NULL,
  `p2_id` BIGINT NOT NULL,
  `p2_name` VARCHAR(255) NOT NULL,
  `p2_chat_id` BIGINT NOT NULL,
  `p2_message_id` BIGINT NULL DEFAULT NULL,
  `p2_private_msg_id` BIGINT NULL DEFAULT NULL,
  `p2_choice` VARCHAR(8) NULL DEFAULT NULL,
  `created_at` DATETIME(6) NOT NULL,
  `expires_at` DATETIME(6) NOT NULL,
  `finished_at` DATETIME(6) NULL DEFAULT NULL,
  `announced_at` DATETIME(6) NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_rps_games_status_expires` (`status`, `expires_at`),
  KEY `idx_rps_games_p1_status` (`p1_id`, `status`),
  KEY `idx_rps_games_p2_status` (`p2_id`, `status`),
  KEY `idx_rps_games_announce` (`announced_at`, `finished_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci"""
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS `rps_games`")
    op.execute("DROP TABLE IF EXISTS `gamble_bets`")
    op.execute("DROP TABLE IF EXISTS `gamble_rounds`")
