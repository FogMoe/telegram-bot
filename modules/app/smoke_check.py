"""启动冒烟检查：`python modules/main.py --check`。

不连接 Telegram 和数据库，只确认依赖可导入、Application 能组装、handler 与 job 能注册。
用于 CI 和镜像构建后的快速验证，不改变正常启动路径。
"""

import sys

from core import config

from .bot_app import create_application

# 组装 Application 只需要 token 的格式，不会发起网络请求。
_PLACEHOLDER_TOKEN = "0:smoke-check"


def run_smoke_check() -> int:
    settings = config.current_settings()
    if not config.TELEGRAM_BOT_TOKEN:
        settings = settings.model_copy(update={"TELEGRAM_BOT_TOKEN": _PLACEHOLDER_TOKEN})

    # 占位 token 只在检查期间生效，不留在进程配置里。
    with config.use_settings(settings):
        application = create_application(settings)

    handler_count = sum(len(handlers) for handlers in application.handlers.values())
    if handler_count == 0:
        print("smoke check failed: no handlers registered", file=sys.stderr)
        return 1

    if application.job_queue is None:
        print(
            "smoke check failed: job queue unavailable "
            "(python-telegram-bot[job-queue] is not installed)",
            file=sys.stderr,
        )
        return 1
    job_count = len(application.job_queue.jobs())

    print(
        f"smoke check ok: {handler_count} handlers, "
        f"{len(application.error_handlers)} error handlers, {job_count} jobs"
    )
    return 0
