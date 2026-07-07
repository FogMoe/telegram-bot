from telegram.ext import (
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from core.bot_commands import (
    admin_announce,
    clear_command,
    error_handler,
    github_command,
    give_command,
    help_command,
    lottery_command,
    me,
    my_chat_member_handler,
    rich_command,
    setmyinfo_command,
    start,
    tl_command,
)
from core.bot_conversation import reply
from core.bot_monitoring import start_monitor, stop_monitor
from features.admin import developer
from features.ai import scheduler
from features.crypto import chart, crypto_predict, swap_fogmoe_solana_token
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
from features.games import gamble, omikuji, rockpaperscissors_game, rpg, sicbo
from features.media import music, pic
from features.moderation import keyword_handler, member_verify, report, spam_control


def register_error_handlers(application) -> None:
    application.add_error_handler(error_handler)


def register_conversation_handlers(application) -> None:
    application.add_handler(CommandHandler("fogmoebot", reply))
    application.add_handler(
        MessageHandler(
            (filters.TEXT | filters.PHOTO | filters.Sticker.ALL)
            & ~filters.COMMAND
            & ~filters.VIA_BOT
            & (filters.UpdateType.MESSAGE | filters.UpdateType.EDITED_MESSAGE),
            reply,
        )
    )


def register_core_command_handlers(application) -> None:
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("me", me))
    application.add_handler(CommandHandler("lottery", lottery_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("github", github_command))
    application.add_handler(CommandHandler("clear", clear_command))
    application.add_handler(CommandHandler("admin_announce", admin_announce))
    application.add_handler(CommandHandler("setmyinfo", setmyinfo_command))
    application.add_handler(CommandHandler("give", give_command))
    bribe.setup_bribe_command(application)


def register_monitoring_handlers(application) -> None:
    application.add_handler(CommandHandler("start_test_monitor", start_monitor))
    application.add_handler(CommandHandler("stop_test_monitor", stop_monitor))


def register_interactive_feature_handlers(application) -> None:
    # 内联翻译暂时禁用。
    # application.add_handler(InlineQueryHandler(inline_translate))

    application.add_handler(CommandHandler("gamble", gamble.gamble_command))
    application.add_handler(
        CallbackQueryHandler(gamble.gamble_callback, pattern=r"^gamble_")
    )

    application.add_handler(CommandHandler("shop", shop.shop_command))
    application.add_handler(CallbackQueryHandler(shop.shop_callback, pattern=r"^shop_"))
    application.job_queue.run_repeating(
        shop.cleanup_message_records_job,
        interval=3600,
        first=10,
    )

    application.add_handler(CommandHandler("task", task.task_command))
    application.add_handler(CallbackQueryHandler(task.task_callback, pattern=r"^task_"))

    application.add_handler(CommandHandler("rich", rich_command))


def register_membership_handlers(application) -> None:
    member_verify.setup_member_verification(application)
    application.add_handler(
        ChatMemberHandler(
            my_chat_member_handler,
            chat_member_types=ChatMemberHandler.MY_CHAT_MEMBER,
        )
    )


def register_staking_and_crypto_handlers(application) -> None:
    stake_coin.setup_stake_handlers(application)
    crypto_predict.setup_crypto_predict_handlers(application)
    swap_fogmoe_solana_token.setup_swap_handler(application)


def register_translation_handlers(application) -> None:
    application.add_handler(CommandHandler("tl", tl_command))


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
    application.add_handler(CommandHandler("rpg", rpg.rpg_command_handler))


def register_admin_handlers(application) -> None:
    developer.setup_developer_handlers(application)
    web_password.setup_webpassword_handlers(application)


def register_ai_jobs(application) -> None:
    application.job_queue.run_repeating(
        scheduler.run_ai_schedule_job,
        interval=scheduler.SCHEDULE_POLL_INTERVAL,
        first=5,
    )
