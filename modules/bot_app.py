from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    InlineQueryHandler,
    MessageHandler,
    filters,
)

import bribe
import charge_coin
import chart
import checkin
import config
import crypto_predict
import developer
import gamble
import keyword_handler
import member_verify
import music
import omikuji
import pic
import ref
import report
import rockpaperscissors_game
import rpg
import shop
import sicbo
import spam_control
import stake_coin
import swap_fogmoe_solana_token
import task
import web_password

from bot_commands import (
    admin_announce,
    clear_command,
    error_handler,
    github_command,
    give_command,
    help_command,
    inline_translate,
    lottery_command,
    me,
    my_chat_member_handler,
    rich_command,
    setmyinfo_command,
    start,
    tl_command,
)
from bot_conversation import post_init, reply
from bot_monitoring import start_monitor, stop_monitor


def create_application():
    application = ApplicationBuilder() \
        .token(config.TELEGRAM_BOT_TOKEN) \
        .concurrent_updates(True) \
        .post_init(post_init) \
        .build()

    register_handlers(application)
    return application


def register_handlers(application) -> None:
    application.add_error_handler(error_handler)
    application.add_handler(CommandHandler("fogmoebot", reply))  # Call bot at group
    message_handler = MessageHandler(
        (filters.TEXT | filters.PHOTO | filters.Sticker.ALL) &
        ~filters.COMMAND &
        ~filters.VIA_BOT &
        (filters.UpdateType.MESSAGE | filters.UpdateType.EDITED_MESSAGE),
        reply,
    )
    application.add_handler(message_handler)
    start_handler = CommandHandler('start', start)
    application.add_handler(start_handler)
    me_handler = CommandHandler('me', me)
    application.add_handler(me_handler)
    lottery_handler = CommandHandler('lottery', lottery_command)
    application.add_handler(lottery_handler)
    help_handler = CommandHandler('help', help_command)
    application.add_handler(help_handler)
    github_handler = CommandHandler('github', github_command)
    application.add_handler(github_handler)
    clear_handler = CommandHandler('clear', clear_command)
    application.add_handler(clear_handler)
    admin_announce_handler = CommandHandler('admin_announce', admin_announce)
    application.add_handler(admin_announce_handler)
    setmyinfo_handler = CommandHandler('setmyinfo', setmyinfo_command)
    application.add_handler(setmyinfo_handler)
    give_handler = CommandHandler("give", give_command)
    application.add_handler(give_handler)
    bribe.setup_bribe_command(application)

    # 添加监控命令
    application.add_handler(CommandHandler("start_test_monitor", start_monitor))
    application.add_handler(CommandHandler("stop_test_monitor", stop_monitor))

    # 添加内联翻译处理程序（暂时禁用）
    # application.add_handler(InlineQueryHandler(inline_translate))

    # 添加赌博命令和回调处理
    application.add_handler(CommandHandler("gamble", gamble.gamble_command))
    application.add_handler(CallbackQueryHandler(gamble.gamble_callback, pattern=r"^gamble_"))

    # 商店
    shop_handler = CommandHandler("shop", shop.shop_command)
    application.add_handler(shop_handler)
    application.add_handler(CallbackQueryHandler(shop.shop_callback, pattern=r"^shop_"))
    # 使用job_queue替代直接创建任务
    application.job_queue.run_repeating(shop.cleanup_message_records_job, interval=3600, first=10)

    # 任务
    task_handler = CommandHandler("task", task.task_command)
    application.add_handler(task_handler)
    application.add_handler(CallbackQueryHandler(task.task_callback, pattern=r"^task_"))

    # 添加富豪榜指令
    rich_handler = CommandHandler("rich", rich_command)
    application.add_handler(rich_handler)

    # 注册 member_verify 模块的处理器
    member_verify.setup_member_verification(application)

    # 添加处理新群组成员的 handler
    application.add_handler(ChatMemberHandler(my_chat_member_handler, chat_member_types=ChatMemberHandler.MY_CHAT_MEMBER))

    # 添加质押系统处理器
    stake_coin.setup_stake_handlers(application)

    # 添加加密货币预测处理器
    crypto_predict.setup_crypto_predict_handlers(application)

    # 添加代币兑换处理器
    swap_fogmoe_solana_token.setup_swap_handler(application)

    # 添加翻译命令处理器
    tl_handler = CommandHandler('tl', tl_command)
    application.add_handler(tl_handler)

    # 添加关键词处理器
    keyword_handler.setup_keyword_handlers(application)

    # 添加垃圾信息过滤处理器
    spam_control.setup_spam_control_handlers(application)

    # 添加御神签模块处理器
    omikuji.setup_omikuji_handlers(application)

    # 添加石头剪刀布游戏处理器
    rockpaperscissors_game.setup_rps_game_handlers(application)

    # 添加充值系统处理器
    charge_coin.setup_charge_handlers(application)

    # 添加SICBO骰宝游戏处理器
    sicbo.setup_sicbo_handlers(application)

    # 注册推广系统的处理器
    ref.setup_ref_handlers(application)

    # 注册每日签到系统的处理器
    checkin.setup_checkin_handlers(application)

    # 注册举报系统的处理器
    report.setup_report_handlers(application)

    # 注册代币图表模块处理器
    chart.setup_chart_handlers(application)

    # 注册图片模块处理器
    pic.setup_pic_handlers(application)

    # 注册分享链接检测模块处理器 （暂时关闭）
    # sf.setup_sf_handlers(application)

    # 注册音乐搜索模块处理器
    music.setup_music_handlers(application)

    # 注册RPG游戏模块处理器
    application.add_handler(CommandHandler("rpg", rpg.rpg_command_handler))

    # 注册开发者命令模块处理器
    developer.setup_developer_handlers(application)

    # 注册Web密码模块处理器
    web_password.setup_webpassword_handlers(application)


def run() -> None:
    application = create_application()
    application.run_polling()
