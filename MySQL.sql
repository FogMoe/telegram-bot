CREATE TABLE `chat_records` (
  `id` int NOT NULL,
  `conversation_id` bigint NOT NULL,
  `messages` json NOT NULL,
  `timestamp` timestamp NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE IF NOT EXISTS `permanent_chat_records` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `user_id` BIGINT NOT NULL,
  `conversation_snapshot` JSON NOT NULL,
  `summary` TEXT NULL DEFAULT NULL,
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  INDEX `idx_permanent_user_created` (`user_id`, `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE IF NOT EXISTS `chat_records_group` (
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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

-- =========================================================
-- User 表：兼容 Telegram / 本地账号
-- =========================================================
CREATE TABLE `user` (
  `id`        BIGINT NOT NULL,                           -- 主键：Telegram UID 或站内自增ID
  `tg_uid`    BIGINT NULL UNIQUE,                       -- Telegram UID；本地账号留 NULL
  `provider`  ENUM('telegram','local','web') NOT NULL DEFAULT 'telegram',
  `name`      TEXT COLLATE utf8mb4_general_ci NOT NULL,
  `coins`     INT  NOT NULL DEFAULT 0,
  `permission`INT           DEFAULT 0,
  `info`      VARCHAR(500)  DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE `user_lottery` (
  `user_id` bigint NOT NULL,
  `last_lottery_date` datetime DEFAULT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE `user_task` (
  `user_id` BIGINT NOT NULL,
  `task_id` INT NOT NULL,
  `completed_date` DATETIME DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`user_id`, `task_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE `group_verification` (
  `group_id` BIGINT NOT NULL,
  `group_name` TEXT COLLATE utf8mb4_general_ci NOT NULL,
  PRIMARY KEY (`group_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE `verification_tasks` (
  `user_id` BIGINT NOT NULL,
  `group_id` BIGINT NOT NULL,
  `message_id` BIGINT NOT NULL,
  `expire_time` DATETIME NOT NULL,
  PRIMARY KEY (`user_id`, `group_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE `user_stakes` (
  `user_id` BIGINT NOT NULL,
  `stake_amount` INT NOT NULL,
  `stake_time` DATETIME NOT NULL,
  `last_reward_time` DATETIME NULL,
  PRIMARY KEY (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE `user_btc_predictions` (
  `user_id` BIGINT NOT NULL,
  `predict_type` VARCHAR(10) NOT NULL,
  `amount` INT NOT NULL,
  `start_price` DECIMAL(20,8) NOT NULL,
  `start_time` DATETIME NOT NULL,
  `end_time` DATETIME NOT NULL,
  `is_completed` BOOLEAN DEFAULT FALSE,
  PRIMARY KEY (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE `token_swap_requests` (
  `id` INT AUTO_INCREMENT PRIMARY KEY,
  `user_id` BIGINT NOT NULL,
  `username` VARCHAR(255) NOT NULL,
  `wallet_address` VARCHAR(50) NOT NULL,
  `amount` INT NOT NULL,
  `request_time` DATETIME DEFAULT CURRENT_TIMESTAMP,
  `status` VARCHAR(20) DEFAULT 'pending'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE IF NOT EXISTS `group_keywords` (
  `id` INT AUTO_INCREMENT PRIMARY KEY,
  `group_id` BIGINT NOT NULL,
  `keyword` VARCHAR(255) NOT NULL,
  `response` TEXT NOT NULL,
  `created_by` BIGINT NOT NULL,
  `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY `group_keyword_unique` (`group_id`, `keyword`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE `group_spam_control` (
  `group_id` BIGINT NOT NULL,
  `enabled` BOOLEAN NOT NULL DEFAULT FALSE,
  `enabled_by` BIGINT,
  `enabled_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
  `updated_at` DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`group_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE IF NOT EXISTS `group_spam_keywords` (
  `id` INT AUTO_INCREMENT PRIMARY KEY,
  `group_id` BIGINT NOT NULL,
  `keyword` VARCHAR(255) NOT NULL,
  `is_regex` BOOLEAN NOT NULL DEFAULT FALSE,
  `created_by` BIGINT NOT NULL,
  `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY `group_spam_keyword_unique` (`group_id`, `keyword`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE `user_omikuji` (
  `user_id` bigint NOT NULL,
  `fortune_date` DATE NOT NULL,
  `fortune` VARCHAR(10) NOT NULL,
  `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`user_id`, `fortune_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

ALTER TABLE `group_spam_control` 
ADD COLUMN `block_links` BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE `group_spam_control` 
ADD COLUMN `block_mentions` BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE `redemption_codes` (
  `id` INT AUTO_INCREMENT PRIMARY KEY,
  `code` VARCHAR(255) NOT NULL UNIQUE,
  `amount` INT NOT NULL,
  `is_used` BOOLEAN NOT NULL DEFAULT FALSE,
  `used_by` BIGINT DEFAULT NULL,
  `used_at` DATETIME DEFAULT NULL,
  FOREIGN KEY (used_by) REFERENCES user(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

-- 邀请系统表
CREATE TABLE `user_invitations` (
  `invited_user_id` BIGINT NOT NULL,
  `referrer_id` BIGINT NOT NULL,
  `invitation_time` DATETIME DEFAULT CURRENT_TIMESTAMP,
  `reward_claimed` BOOLEAN DEFAULT FALSE,
  PRIMARY KEY (`invited_user_id`),
  FOREIGN KEY (`invited_user_id`) REFERENCES `user`(`id`) ON DELETE CASCADE,
  FOREIGN KEY (`referrer_id`) REFERENCES `user`(`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

-- 机器人添加到群组记录表（暂时无效）
-- CREATE TABLE `user_group_additions` (
--   `user_id` BIGINT NOT NULL,
--   `group_id` BIGINT NOT NULL,
--   `group_name` VARCHAR(255),
--   `member_count` INT DEFAULT 0,
--   `added_time` DATETIME DEFAULT CURRENT_TIMESTAMP,
--   `reward_claimed` BOOLEAN DEFAULT FALSE,
--   PRIMARY KEY (`user_id`, `group_id`),
--   FOREIGN KEY (`user_id`) REFERENCES `user`(`id`) ON DELETE CASCADE
-- ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE IF NOT EXISTS `user_checkin` (
  `user_id` BIGINT NOT NULL,
  `last_checkin_date` DATE NOT NULL,
  `consecutive_days` INT NOT NULL DEFAULT 1,
  `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
  `updated_at` DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE IF NOT EXISTS `group_chart_tokens` (
  `group_id` BIGINT NOT NULL,
  `chain` VARCHAR(20) NOT NULL,
  `ca` VARCHAR(100) NOT NULL,
  `set_by` BIGINT NOT NULL,
  `buy_alert_threshold` FLOAT NULL,
  `set_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
  `updated_at` DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`group_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE rpg_characters (
    character_id INT AUTO_INCREMENT PRIMARY KEY,
    user_id BIGINT UNIQUE NOT NULL,
    level INT DEFAULT 1,
    hp INT DEFAULT 10,
    max_hp INT DEFAULT 10, -- 添加最大生命值以便战斗后恢复
    atk INT DEFAULT 2,
    matk INT DEFAULT 0,
    def INT DEFAULT 1,
    experience BIGINT DEFAULT 0,
    allow_battle BOOLEAN DEFAULT TRUE, -- 默认允许被挑战
    FOREIGN KEY (user_id) REFERENCES user(id) ON DELETE CASCADE
);

-- RPG 装备表
CREATE TABLE rpg_equipment (
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
);

-- RPG 道具表
CREATE TABLE rpg_items (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(50) NOT NULL,
    type ENUM('consumable', 'material', 'quest') NOT NULL,
    effect TEXT,
    description TEXT,
    price INT NOT NULL,
    use_limit INT DEFAULT 1,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 玩家装备表
CREATE TABLE rpg_player_equipment (
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
);

-- 玩家道具表
CREATE TABLE rpg_player_inventory (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id BIGINT NOT NULL,
    item_id INT NOT NULL,
    quantity INT DEFAULT 1,
    FOREIGN KEY (user_id) REFERENCES user(id) ON DELETE CASCADE,
    FOREIGN KEY (item_id) REFERENCES rpg_items(id) ON DELETE CASCADE,
    UNIQUE KEY (user_id, item_id)
);

-- RPG 商店表
CREATE TABLE rpg_shop (
    id INT AUTO_INCREMENT PRIMARY KEY,
    item_type ENUM('equipment', 'item') NOT NULL,
    item_id INT NOT NULL,
    price INT NOT NULL,
    stock INT DEFAULT -1, -- -1表示无限库存
    available BOOLEAN DEFAULT TRUE,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX (item_type, item_id),
    UNIQUE KEY (item_type, item_id)
);

-- 玩家装备统计缓存表（用于提高性能，避免频繁计算）
CREATE TABLE rpg_player_equipment_stats (
    user_id BIGINT NOT NULL,
    total_atk_bonus INT DEFAULT 0,
    total_def_bonus INT DEFAULT 0,
    total_hp_bonus INT DEFAULT 0,
    total_matk_bonus INT DEFAULT 0,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES user(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id)
);

-- Web密码表
CREATE TABLE web_password (
    user_id BIGINT NOT NULL,
    password VARCHAR(255) NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES user(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE IF NOT EXISTS `kindness_gifts` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `donor_id` BIGINT NOT NULL,
  `recipient_id` BIGINT NOT NULL,
  `amount` INT NOT NULL,
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uniq_donor_recipient_day` (`donor_id`, `recipient_id`, `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
CREATE TABLE IF NOT EXISTS `kindness_gifts` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `donor_id` BIGINT NOT NULL,
  `recipient_id` BIGINT NOT NULL,
  `amount` INT NOT NULL,
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uniq_donor_day` (`donor_id`, `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE IF NOT EXISTS `ai_user_affection` (
  `user_id` BIGINT NOT NULL,
  `affection` INT NOT NULL DEFAULT 0,
  `impression` VARCHAR(500),
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`user_id`),
  CONSTRAINT `fk_ai_user_affection_user` FOREIGN KEY (`user_id`) REFERENCES `user`(`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
