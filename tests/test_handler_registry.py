from telegram.ext import (
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    MessageHandler,
)

from app.handler_registry import REGISTRATION_STEPS, register_handlers


class FakeJobQueue:
    def __init__(self):
        self.jobs = []

    def run_repeating(self, callback, interval, first=None):
        self.jobs.append(
            {
                "callback": callback,
                "interval": interval,
                "first": first,
            }
        )


class FakeApplication:
    def __init__(self):
        self.handlers = []
        self.error_handlers = []
        self.job_queue = FakeJobQueue()

    def add_handler(self, handler, group=0):
        self.handlers.append(
            {
                "handler": handler,
                "group": group,
            }
        )

    def add_error_handler(self, handler):
        self.error_handlers.append(handler)


def _handler_signature(handler, group):
    if isinstance(handler, CommandHandler):
        detail = ",".join(sorted(handler.commands))
    elif isinstance(handler, CallbackQueryHandler):
        detail = handler.pattern.pattern if handler.pattern else ""
    elif isinstance(handler, ChatMemberHandler):
        detail = str(handler.chat_member_types)
    elif isinstance(handler, MessageHandler):
        detail = type(handler.filters).__name__
    else:
        detail = ""

    return (
        type(handler).__name__,
        group,
        detail,
        handler.callback.__name__,
    )


def _job_signature(job):
    return (
        getattr(job["callback"], "__name__", repr(job["callback"])),
        job["interval"],
        job["first"],
    )


def test_registration_steps_are_grouped_by_app_assembly_boundary():
    assert [step.__name__ for step in REGISTRATION_STEPS] == [
        "register_error_handlers",
        "register_conversation_handlers",
        "register_core_command_handlers",
        "register_monitoring_handlers",
        "register_interactive_feature_handlers",
        "register_membership_handlers",
        "register_staking_and_crypto_handlers",
        "register_translation_handlers",
        "register_moderation_handlers",
        "register_game_and_recharge_handlers",
        "register_economy_handlers",
        "register_reporting_handlers",
        "register_media_and_chart_handlers",
        "register_rpg_handlers",
        "register_admin_handlers",
        "register_ai_jobs",
    ]


def test_register_handlers_preserves_handler_and_job_registration_order():
    application = FakeApplication()

    register_handlers(application)

    assert [handler.__name__ for handler in application.error_handlers] == [
        "error_handler"
    ]
    assert [
        _handler_signature(entry["handler"], entry["group"])
        for entry in application.handlers
    ] == [
        ("CommandHandler", 0, "fogmoebot", "reply"),
        ("MessageHandler", 0, "_MergedFilter", "reply"),
        ("CommandHandler", 0, "start", "start"),
        ("CommandHandler", 0, "me", "me"),
        ("CommandHandler", 0, "lottery", "lottery_command"),
        ("CommandHandler", 0, "help", "help_command"),
        ("CommandHandler", 0, "github", "github_command"),
        ("CommandHandler", 0, "clear", "clear_command"),
        ("CommandHandler", 0, "admin_announce", "admin_announce"),
        ("CommandHandler", 0, "setmyinfo", "setmyinfo_command"),
        ("CommandHandler", 0, "give", "give_command"),
        ("CommandHandler", 0, "start_test_monitor", "start_monitor"),
        ("CommandHandler", 0, "stop_test_monitor", "stop_monitor"),
        ("CommandHandler", 0, "gamble", "gamble_command"),
        ("CallbackQueryHandler", 0, "^gamble_", "gamble_callback"),
        ("CommandHandler", 0, "shop", "shop_command"),
        ("CallbackQueryHandler", 0, "^shop_", "shop_callback"),
        ("CommandHandler", 0, "task", "task_command"),
        ("CallbackQueryHandler", 0, "^task_", "task_callback"),
        ("CommandHandler", 0, "rich", "rich_command"),
        ("CommandHandler", 0, "verify", "verify_command"),
        ("MessageHandler", 0, "_NewChatMembers", "new_member_handler"),
        ("CallbackQueryHandler", 0, "^verify_", "verify_callback"),
        ("MessageHandler", 0, "_LeftChatMember", "handle_member_left"),
        ("ChatMemberHandler", 0, "-1", "my_chat_member_handler"),
        ("CommandHandler", 0, "stake", "stake_command"),
        ("CallbackQueryHandler", 0, "^stake_", "stake_callback"),
        ("CommandHandler", 0, "btc_predict", "btc_predict_command"),
        ("CallbackQueryHandler", 0, "^crypto_", "crypto_callback"),
        ("CommandHandler", 0, "swap", "swap_command"),
        ("CommandHandler", 0, "tl", "tl_command"),
        ("CommandHandler", 0, "keyword", "keyword_command"),
        ("MessageHandler", 10, "_MergedFilter", "process_group_message"),
        ("CommandHandler", 0, "spam", "toggle_spam_control"),
        ("CallbackQueryHandler", 0, "^spam_help$", "spam_help_callback"),
        ("MessageHandler", 5, "_MergedFilter", "process_message"),
        ("CommandHandler", 0, "omikuji", "omikuji_command"),
        ("CallbackQueryHandler", 0, "^omikuji_", "omikuji_callback"),
        ("CommandHandler", 0, "rps_game", "rps_game_command"),
        ("CallbackQueryHandler", 0, "^rps_", "rps_callback_handler"),
        ("CommandHandler", 0, "charge", "charge_command"),
        ("CommandHandler", 0, "create_code", "admin_create_code"),
        ("CommandHandler", 0, "recharge", "recharge_command"),
        ("CallbackQueryHandler", 0, "^topup_req_", "topup_request_callback"),
        ("CallbackQueryHandler", 0, "^topup_admin_", "topup_admin_callback"),
        ("CommandHandler", 0, "sicbo", "sicbo_command"),
        ("CallbackQueryHandler", 0, "^sicbo_", "handle_callback"),
        ("CommandHandler", 0, "ref", "ref_command"),
        ("CallbackQueryHandler", 0, "^ref_", "ref_callback"),
        ("CommandHandler", 0, "checkin", "checkin_command"),
        ("CommandHandler", 0, "report", "report_command"),
        ("CommandHandler", 0, "chart", "chart_command"),
        ("CommandHandler", 0, "pic", "pic_command"),
        ("CallbackQueryHandler", 0, "^pic_hd_", "hd_pic_callback"),
        ("CommandHandler", 0, "music", "music_command"),
        ("CallbackQueryHandler", 0, "^music_", "music_platform_callback"),
        ("CommandHandler", 0, "rpg", "rpg_command_handler"),
        ("CommandHandler", 0, "stats", "get_bot_stats"),
        ("CommandHandler", 0, "logs", "view_logs"),
        ("CommandHandler", 0, "webpassword", "webpassword_command"),
    ]
    assert [_job_signature(job) for job in application.job_queue.jobs] == [
        ("cleanup_message_records_job", 3600, 10),
        ("cleanup_expired_games", 300, None),
        ("refresh_cache_job", 1800, 10),
        ("<lambda>", 3600, 1800),
        ("clean_expired_requests_job", 300, 10),
        ("run_ai_schedule_job", 60, 5),
    ]
