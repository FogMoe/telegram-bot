"""Add FOGMOE account bindings and short-lived OAuth transactions."""

from alembic import op

revision = "0015_add_fogmoe_account_bindings"
down_revision = "0014_add_ai_user_diary_page_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE `fogmoe_account_bindings` (
          `id` BIGINT NOT NULL AUTO_INCREMENT,
          `telegram_user_id` BIGINT NOT NULL,
          `fogmoe_subject` CHAR(36) NOT NULL,
          `fogmoe_username` VARCHAR(100) NULL,
          `created_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
          `last_verified_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
          `unbound_at` DATETIME(6) NULL,
          `active_telegram_user_id` BIGINT
            GENERATED ALWAYS AS (
              CASE WHEN `unbound_at` IS NULL THEN `telegram_user_id` ELSE NULL END
            ) STORED,
          `active_fogmoe_subject` CHAR(36)
            GENERATED ALWAYS AS (
              CASE WHEN `unbound_at` IS NULL THEN `fogmoe_subject` ELSE NULL END
            ) STORED,
          PRIMARY KEY (`id`),
          UNIQUE KEY `uq_fogmoe_bindings_active_telegram`
            (`active_telegram_user_id`),
          UNIQUE KEY `uq_fogmoe_bindings_active_subject`
            (`active_fogmoe_subject`),
          KEY `idx_fogmoe_bindings_telegram_history`
            (`telegram_user_id`, `created_at`),
          KEY `idx_fogmoe_bindings_subject_history`
            (`fogmoe_subject`, `created_at`),
          CONSTRAINT `fk_fogmoe_account_bindings_user`
            FOREIGN KEY (`telegram_user_id`) REFERENCES `user` (`id`)
            ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
        """
    )
    op.execute(
        """
        CREATE TABLE `fogmoe_binding_reward_claims` (
          `telegram_user_id` BIGINT NOT NULL,
          `fogmoe_subject` CHAR(36) NOT NULL,
          `claimed_at` DATETIME(6) NOT NULL,
          PRIMARY KEY (`telegram_user_id`),
          UNIQUE KEY `uq_fogmoe_reward_claims_subject` (`fogmoe_subject`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
        """
    )
    op.execute(
        """
        CREATE TABLE `fogmoe_oauth_transactions` (
          `state_hash` CHAR(64) NOT NULL,
          `telegram_user_id` BIGINT NOT NULL,
          `chat_id` BIGINT NOT NULL,
          `code_verifier` VARCHAR(128) NOT NULL,
          `nonce` VARCHAR(128) NOT NULL,
          `redirect_uri` VARCHAR(2048) NOT NULL,
          `requested_scopes` VARCHAR(255) NOT NULL,
          `action` ENUM('bind','unbind') NOT NULL,
          `expected_subject` CHAR(36) NULL,
          `expires_at` DATETIME(6) NOT NULL,
          `consumed_at` DATETIME(6) NULL,
          `created_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
          PRIMARY KEY (`state_hash`),
          CONSTRAINT `chk_fogmoe_oauth_transactions_action`
            CHECK (
              (`action` = 'bind' AND `expected_subject` IS NULL)
              OR (`action` = 'unbind' AND `expected_subject` IS NOT NULL)
            ),
          KEY `idx_fogmoe_oauth_transactions_user` (`telegram_user_id`),
          KEY `idx_fogmoe_oauth_transactions_expiry` (`expires_at`),
          CONSTRAINT `fk_fogmoe_oauth_transactions_user`
            FOREIGN KEY (`telegram_user_id`) REFERENCES `user` (`id`)
            ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS `fogmoe_oauth_transactions`")
    op.execute("DROP TABLE IF EXISTS `fogmoe_binding_reward_claims`")
    op.execute("DROP TABLE IF EXISTS `fogmoe_account_bindings`")
