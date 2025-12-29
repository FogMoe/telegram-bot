"""Initial schema migration."""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE `chat_records` (
  `id` int NOT NULL,
  `conversation_id` bigint NOT NULL,
  `messages` json NOT NULL,
  `timestamp` timestamp NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci""")
    op.execute("""CREATE TABLE IF NOT EXISTS `permanent_chat_records` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `user_id` BIGINT NOT NULL,
  `conversation_snapshot` JSON NOT NULL,
  `summary` TEXT NULL DEFAULT NULL,
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  INDEX `idx_permanent_user_created` (`user_id`, `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci""")
    op.execute("""CREATE TABLE IF NOT EXISTS `chat_records_group` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `group_id` BIGINT NOT NULL,
  `message_id` BIGINT NOT NULL,
  `user_id` BIGINT DEFAULT NULL,
  `message_type` ENUM('text','photo','sticker','voice','video','document','other') NOT NULL DEFAULT 'text',
  `content` TEXT,
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  INDEX `idx_group_created` (`group_id`, `created_at`),
  INDEX `idx_group_message` (`group_id`, `message_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci""")
    op.execute("""CREATE TABLE `user` (
  `id`        BIGINT NOT NULL,
  `tg_uid`    BIGINT NULL UNIQUE,
  `provider`  ENUM('telegram','local','web') NOT NULL DEFAULT 'telegram',
  `name`      TEXT COLLATE utf8mb4_general_ci NOT NULL,
  `coins`     INT  NOT NULL DEFAULT 0,
  `permission`INT           DEFAULT 0,
  `info`      VARCHAR(500)  DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci""")
    op.execute("""CREATE TABLE `user_lottery` (
  `user_id` bigint NOT NULL,
  `last_lottery_date` datetime DEFAULT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci""")
    op.execute("""CREATE TABLE `user_task` (
  `user_id` BIGINT NOT NULL,
  `task_id` INT NOT NULL,
  `completed_date` DATETIME DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`user_id`, `task_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
    op.execute("""CREATE TABLE `group_verification` (
  `group_id` BIGINT NOT NULL,
  `group_name` TEXT COLLATE utf8mb4_general_ci NOT NULL,
  PRIMARY KEY (`group_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci""")
    op.execute("""CREATE TABLE `verification_tasks` (
  `user_id` BIGINT NOT NULL,
  `group_id` BIGINT NOT NULL,
  `message_id` BIGINT NOT NULL,
  `expire_time` DATETIME NOT NULL,
  PRIMARY KEY (`user_id`, `group_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
    op.execute("""CREATE TABLE `user_stakes` (
  `user_id` BIGINT NOT NULL,
  `stake_amount` INT NOT NULL,
  `stake_time` DATETIME NOT NULL,
  `last_reward_time` DATETIME NULL,
  PRIMARY KEY (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
    op.execute("""CREATE TABLE `user_btc_predictions` (
  `user_id` BIGINT NOT NULL,
  `predict_type` VARCHAR(10) NOT NULL,
  `amount` INT NOT NULL,
  `start_price` DECIMAL(20,8) NOT NULL,
  `start_time` DATETIME NOT NULL,
  `end_time` DATETIME NOT NULL,
  `is_completed` BOOLEAN DEFAULT FALSE,
  PRIMARY KEY (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
    op.execute("""CREATE TABLE `token_swap_requests` (
  `id` INT AUTO_INCREMENT PRIMARY KEY,
  `user_id` BIGINT NOT NULL,
  `username` VARCHAR(255) NOT NULL,
  `wallet_address` VARCHAR(50) NOT NULL,
  `amount` INT NOT NULL,
  `request_time` DATETIME DEFAULT CURRENT_TIMESTAMP,
  `status` VARCHAR(20) DEFAULT 'pending'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci""")
    op.execute("""CREATE TABLE IF NOT EXISTS `group_keywords` (
  `id` INT AUTO_INCREMENT PRIMARY KEY,
  `group_id` BIGINT NOT NULL,
  `keyword` VARCHAR(255) NOT NULL,
  `response` TEXT NOT NULL,
  `created_by` BIGINT NOT NULL,
  `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY `group_keyword_unique` (`group_id`, `keyword`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci""")
    op.execute("""CREATE TABLE `group_spam_control` (
  `group_id` BIGINT NOT NULL,
  `enabled` BOOLEAN NOT NULL DEFAULT FALSE,
  `enabled_by` BIGINT,
  `enabled_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
  `updated_at` DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`group_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci""")
    op.execute("""CREATE TABLE IF NOT EXISTS `group_spam_keywords` (
  `id` INT AUTO_INCREMENT PRIMARY KEY,
  `group_id` BIGINT NOT NULL,
  `keyword` VARCHAR(255) NOT NULL,
  `is_regex` BOOLEAN NOT NULL DEFAULT FALSE,
  `created_by` BIGINT NOT NULL,
  `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY `group_spam_keyword_unique` (`group_id`, `keyword`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci""")
    op.execute("""CREATE TABLE `user_omikuji` (
  `user_id` bigint NOT NULL,
  `fortune_date` DATE NOT NULL,
  `fortune` VARCHAR(10) NOT NULL,
  `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`user_id`, `fortune_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci""")
    op.execute("""ALTER TABLE `group_spam_control`
ADD COLUMN `block_links` BOOLEAN NOT NULL DEFAULT FALSE""")
    op.execute("""ALTER TABLE `group_spam_control`
ADD COLUMN `block_mentions` BOOLEAN NOT NULL DEFAULT FALSE""")
    op.execute("""CREATE TABLE `redemption_codes` (
  `id` INT AUTO_INCREMENT PRIMARY KEY,
  `code` VARCHAR(255) NOT NULL UNIQUE,
  `amount` INT NOT NULL,
  `is_used` BOOLEAN NOT NULL DEFAULT FALSE,
  `used_by` BIGINT DEFAULT NULL,
  `used_at` DATETIME DEFAULT NULL,
  FOREIGN KEY (used_by) REFERENCES user(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci""")
    op.execute("""CREATE TABLE `user_invitations` (
  `invited_user_id` BIGINT NOT NULL,
  `referrer_id` BIGINT NOT NULL,
  `invitation_time` DATETIME DEFAULT CURRENT_TIMESTAMP,
  `reward_claimed` BOOLEAN DEFAULT FALSE,
  PRIMARY KEY (`invited_user_id`),
  FOREIGN KEY (`invited_user_id`) REFERENCES `user`(`id`) ON DELETE CASCADE,
  FOREIGN KEY (`referrer_id`) REFERENCES `user`(`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci""")
    op.execute("""CREATE TABLE IF NOT EXISTS `user_checkin` (
  `user_id` BIGINT NOT NULL,
  `last_checkin_date` DATE NOT NULL,
  `consecutive_days` INT NOT NULL DEFAULT 1,
  `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
  `updated_at` DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci""")
    op.execute("""CREATE TABLE IF NOT EXISTS `group_chart_tokens` (
  `group_id` BIGINT NOT NULL,
  `chain` VARCHAR(20) NOT NULL,
  `ca` VARCHAR(100) NOT NULL,
  `set_by` BIGINT NOT NULL,
  `buy_alert_threshold` FLOAT NULL,
  `set_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
  `updated_at` DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`group_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci""")
    op.execute("""CREATE TABLE rpg_characters (
    character_id INT AUTO_INCREMENT PRIMARY KEY,
    user_id BIGINT UNIQUE NOT NULL,
    level INT DEFAULT 1,
    hp INT DEFAULT 10,
    max_hp INT DEFAULT 10,
    atk INT DEFAULT 2,
    matk INT DEFAULT 0,
    def INT DEFAULT 1,
    experience BIGINT DEFAULT 0,
    allow_battle BOOLEAN DEFAULT TRUE,
    FOREIGN KEY (user_id) REFERENCES user(id) ON DELETE CASCADE
)""")
    op.execute("""CREATE TABLE rpg_equipment (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(50) NOT NULL,
    type ENUM('weapon', 'offhand', 'armor', 'treasure1', 'treasure2') NOT NULL,
    atk_bonus INT DEFAULT 0,
    def_bonus INT DEFAULT 0,
    hp_bonus INT DEFAULT 0,
    matk_bonus INT DEFAULT 0,
    description TEXT,
    price INT NOT NULL,
    rarity INT DEFAULT 1,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
)""")
    op.execute("""CREATE TABLE rpg_items (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(50) NOT NULL,
    type ENUM('consumable', 'material', 'quest') NOT NULL,
    effect TEXT,
    description TEXT,
    price INT NOT NULL,
    use_limit INT DEFAULT 1,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
)""")
    op.execute("""CREATE TABLE rpg_player_equipment (
    user_id BIGINT NOT NULL,
    weapon_id INT DEFAULT NULL,
    offhand_id INT DEFAULT NULL,
    armor_id INT DEFAULT NULL,
    treasure1_id INT DEFAULT NULL,
    treasure2_id INT DEFAULT NULL,
    FOREIGN KEY (user_id) REFERENCES user(id) ON DELETE CASCADE,
    FOREIGN KEY (weapon_id) REFERENCES rpg_equipment(id) ON DELETE SET NULL,
    FOREIGN KEY (offhand_id) REFERENCES rpg_equipment(id) ON DELETE SET NULL,
    FOREIGN KEY (armor_id) REFERENCES rpg_equipment(id) ON DELETE SET NULL,
    FOREIGN KEY (treasure1_id) REFERENCES rpg_equipment(id) ON DELETE SET NULL,
    FOREIGN KEY (treasure2_id) REFERENCES rpg_equipment(id) ON DELETE SET NULL,
    PRIMARY KEY (user_id)
)""")
    op.execute("""CREATE TABLE rpg_player_inventory (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id BIGINT NOT NULL,
    item_id INT NOT NULL,
    quantity INT DEFAULT 1,
    FOREIGN KEY (user_id) REFERENCES user(id) ON DELETE CASCADE,
    FOREIGN KEY (item_id) REFERENCES rpg_items(id) ON DELETE CASCADE,
    UNIQUE KEY (user_id, item_id)
)""")
    op.execute("""CREATE TABLE rpg_shop (
    id INT AUTO_INCREMENT PRIMARY KEY,
    item_type ENUM('equipment', 'item') NOT NULL,
    item_id INT NOT NULL,
    price INT NOT NULL,
    stock INT DEFAULT -1,
    available BOOLEAN DEFAULT TRUE,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX (item_type, item_id),
    UNIQUE KEY (item_type, item_id)
)""")
    op.execute("""CREATE TABLE rpg_player_equipment_stats (
    user_id BIGINT NOT NULL,
    total_atk_bonus INT DEFAULT 0,
    total_def_bonus INT DEFAULT 0,
    total_hp_bonus INT DEFAULT 0,
    total_matk_bonus INT DEFAULT 0,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES user(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id)
)""")
    op.execute("""CREATE TABLE web_password (
    user_id BIGINT NOT NULL,
    password VARCHAR(255) NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES user(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci""")
    op.execute("""CREATE TABLE IF NOT EXISTS `kindness_gifts` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `recipient_id` BIGINT NOT NULL,
  `amount` INT NOT NULL,
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_kindness_recipient_created` (`recipient_id`, `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci""")
    op.execute("""CREATE TABLE IF NOT EXISTS `ai_user_affection` (
  `user_id` BIGINT NOT NULL,
  `affection` INT NOT NULL DEFAULT 0,
  `impression` VARCHAR(500),
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`user_id`),
  CONSTRAINT `fk_ai_user_affection_user` FOREIGN KEY (`user_id`) REFERENCES `user`(`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci""")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS `ai_user_affection`")
    op.execute("DROP TABLE IF EXISTS `kindness_gifts`")
    op.execute("DROP TABLE IF EXISTS `web_password`")
    op.execute("DROP TABLE IF EXISTS `rpg_player_equipment_stats`")
    op.execute("DROP TABLE IF EXISTS `rpg_shop`")
    op.execute("DROP TABLE IF EXISTS `rpg_player_inventory`")
    op.execute("DROP TABLE IF EXISTS `rpg_player_equipment`")
    op.execute("DROP TABLE IF EXISTS `rpg_items`")
    op.execute("DROP TABLE IF EXISTS `rpg_equipment`")
    op.execute("DROP TABLE IF EXISTS `rpg_characters`")
    op.execute("DROP TABLE IF EXISTS `group_chart_tokens`")
    op.execute("DROP TABLE IF EXISTS `user_checkin`")
    op.execute("DROP TABLE IF EXISTS `user_invitations`")
    op.execute("DROP TABLE IF EXISTS `redemption_codes`")
    op.execute("DROP TABLE IF EXISTS `user_omikuji`")
    op.execute("DROP TABLE IF EXISTS `group_spam_keywords`")
    op.execute("DROP TABLE IF EXISTS `group_spam_control`")
    op.execute("DROP TABLE IF EXISTS `group_keywords`")
    op.execute("DROP TABLE IF EXISTS `token_swap_requests`")
    op.execute("DROP TABLE IF EXISTS `user_btc_predictions`")
    op.execute("DROP TABLE IF EXISTS `user_stakes`")
    op.execute("DROP TABLE IF EXISTS `verification_tasks`")
    op.execute("DROP TABLE IF EXISTS `group_verification`")
    op.execute("DROP TABLE IF EXISTS `user_task`")
    op.execute("DROP TABLE IF EXISTS `user_lottery`")
    op.execute("DROP TABLE IF EXISTS `user`")
    op.execute("DROP TABLE IF EXISTS `chat_records_group`")
    op.execute("DROP TABLE IF EXISTS `permanent_chat_records`")
    op.execute("DROP TABLE IF EXISTS `chat_records`")
