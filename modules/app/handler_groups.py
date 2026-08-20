"""按功能分组的注册步骤。

这一层只决定注册顺序，不实现业务：每个功能自己提供 `setup_*` 注册函数。
少数命令仍在这里直接 `add_handler`，因为它们的注册顺序在历史上是跨功能
交错的，而 `tests/test_handler_registry.py` 把最终顺序当作契约。
"""

from telegram.ext import CommandHandler

from features.admin import developer
from features.admin.announce import admin_announce
from features.ai import idle_followup, scheduler, translate_handlers
from features.conversation import handlers as conversation
from features.conversation import history_hooks
from features.conversation.clear import clear_command
from features.crypto import chart, crypto_predict, monitoring, swap_fogmoe_solana_token
from features.economy import (
    bribe,
    charge_coin,
    checkin,
    ref,
    shop,
    stake_coin,
    task,
    web_password,
)
from features.economy.coins import give_command, lottery_command, rich_command
from features.games import gamble, omikuji, rockpaperscissors_game, rpg, sicbo
from features.media import music, pic
from features.moderation import keyword_handler, member_verify, report, spam_control
from features.profile import handlers as profile

from .error_handler import error_handler


def register_error_handlers(application) -> None:
    application.add_error_handler(error_handler)


def register_history_handlers(application) -> None:
    history_hooks.setup_history_handlers(application)


def register_conversation_handlers(application) -> None:
    conversation.setup_conversation_handlers(application)


def register_core_command_handlers(application) -> None:
    application.add_handler(CommandHandler("start", profile.start))
    application.add_handler(CommandHandler("me", profile.me))
    application.add_handler(CommandHandler("lottery", lottery_command))
    application.add_handler(CommandHandler("help", profile.help_command))
    application.add_handler(CommandHandler("github", profile.github_command))
    application.add_handler(CommandHandler("clear", clear_command))
    application.add_handler(CommandHandler("admin_announce", admin_announce))
    application.add_handler(CommandHandler("setmyinfo", profile.setmyinfo_command))
    application.add_handler(CommandHandler("give", give_command))
    bribe.setup_bribe_command(application)


def register_monitoring_handlers(application) -> None:
    monitoring.setup_monitor_handlers(application)


def register_interactive_feature_handlers(application) -> None:
    # 内联翻译暂时禁用。
    # application.add_handler(InlineQueryHandler(translate_handlers.inline_translate))

    gamble.setup_gamble_handlers(application)
    shop.setup_shop_handlers(application)
    task.setup_task_handlers(application)
    application.add_handler(CommandHandler("rich", rich_command))


def register_membership_handlers(application) -> None:
    member_verify.setup_member_verification(application)
    profile.setup_membership_handlers(application)


def register_staking_and_crypto_handlers(application) -> None:
    stake_coin.setup_stake_handlers(application)
    crypto_predict.setup_crypto_predict_handlers(application)
    swap_fogmoe_solana_token.setup_swap_handler(application)


def register_translation_handlers(application) -> None:
    translate_handlers.setup_translation_handlers(application)


def register_moderation_handlers(application) -> None:
    keyword_handler.setup_keyword_handlers(application)
    spam_control.setup_spam_control_handlers(application)


def register_game_and_recharge_handlers(application) -> None:
    omikuji.setup_omikuji_handlers(application)
    rockpaperscissors_game.setup_rps_game_handlers(application)
    charge_coin.setup_charge_handlers(application)
    sicbo.setup_sicbo_handlers(application)


def register_economy_handlers(application) -> None:
    ref.setup_ref_handlers(application)
    checkin.setup_checkin_handlers(application)


def register_reporting_handlers(application) -> None:
    report.setup_report_handlers(application)


def register_media_and_chart_handlers(application) -> None:
    chart.setup_chart_handlers(application)
    pic.setup_pic_handlers(application)

    # 分享链接检测暂时关闭。
    # sf.setup_sf_handlers(application)

    music.setup_music_handlers(application)


def register_rpg_handlers(application) -> None:
    rpg.setup_rpg_handlers(application)


def register_admin_handlers(application) -> None:
    developer.setup_developer_handlers(application)
    web_password.setup_webpassword_handlers(application)


def register_ai_jobs(application) -> None:
    scheduler.setup_schedule_jobs(application)
    idle_followup.setup_idle_followup_jobs(application)
