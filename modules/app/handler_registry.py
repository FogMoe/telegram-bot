from .handler_groups import (
    register_admin_handlers,
    register_ai_jobs,
    register_conversation_handlers,
    register_core_command_handlers,
    register_economy_handlers,
    register_error_handlers,
    register_game_and_recharge_handlers,
    register_interactive_feature_handlers,
    register_media_and_chart_handlers,
    register_membership_handlers,
    register_moderation_handlers,
    register_monitoring_handlers,
    register_reporting_handlers,
    register_rpg_handlers,
    register_staking_and_crypto_handlers,
    register_translation_handlers,
)


REGISTRATION_STEPS = (
    register_error_handlers,
    register_conversation_handlers,
    register_core_command_handlers,
    register_monitoring_handlers,
    register_interactive_feature_handlers,
    register_membership_handlers,
    register_staking_and_crypto_handlers,
    register_translation_handlers,
    register_moderation_handlers,
    register_game_and_recharge_handlers,
    register_economy_handlers,
    register_reporting_handlers,
    register_media_and_chart_handlers,
    register_rpg_handlers,
    register_admin_handlers,
    register_ai_jobs,
)


def register_handlers(application) -> None:
    for register_step in REGISTRATION_STEPS:
        register_step(application)
