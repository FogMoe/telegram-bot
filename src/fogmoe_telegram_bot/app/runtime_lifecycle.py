"""进程运行时的启动与关停顺序：准入、后台任务、HTTP 客户端、线程适配器、数据库引擎。

PTB 的停止流程是：停止拉取 update -> 等在途的 handler 与 job 结束 -> `post_stop`。因此：

- `begin_shutdown()` 在 PTB 开始停止的第一刻调用（`BotApplication.stop`）：不再接受新的准入，
  排队的轮次立刻被拒绝（还没扣费），在途的轮次获得 `RUNTIME_SHUTDOWN_GRACE_SECONDS` 的宽限，
  之后截止时间到期，用户收到「正在重启」的提示。否则 PTB 会一直等到每一轮自己的截止时间。
- `shutdown_runtime()` 在 `post_stop` 里按固定顺序释放资源，每一步独立 try，失败只记录：

  1. 停止接收新工作：准入关闭，取消宽限计时器；
  2. 取消并等待挂起任务：后台摘要、指标汇总、行情监控等经 `core.background` 登记的任务；
  3. 把还没写完的 Telegram 历史事件刷进数据库（此时数据库还开着）；
  4. 关闭 HTTP 客户端：LiteLLM 的异步客户端、同步工具登记的 requests 会话；
  5. 关闭线程适配器：取消排队中的同步调用，释放线程；
  6. 关闭数据库连接池。

  E 的「关停释放未开始的 claim」语义不在这里：定时任务与空闲跟进在拿到会话锁之后检查
  `Application.running`，已经停止就释放尚在 `claimed` 阶段的 claim（见 docs/job-recovery.md）。
  PTB 的 `stop()` 会等它们结束，所以走到 `post_stop` 时它们已经完成释放。
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable

from fogmoe_telegram_bot.core import background, blocking, config, db, http_sessions
from fogmoe_telegram_bot.core.admission import get_admission
from fogmoe_telegram_bot.core.metrics import MetricsReporter
from fogmoe_telegram_bot.core.telegram_history import flush_all_pending_events
from fogmoe_telegram_bot.features.ai import litellm_client

logger = logging.getLogger(__name__)


def start_runtime() -> None:
    """启动运行时：重新打开后台任务与线程适配器，启动指标汇总。在 `post_init` 里调用。"""
    background.BACKGROUND.reopen()
    blocking.reopen_all()
    get_admission().reopen()

    interval = float(config.RUNTIME_METRICS_LOG_INTERVAL_SECONDS)
    if interval > 0:
        background.spawn(MetricsReporter(interval).run(), name="metrics-reporter")


def begin_shutdown() -> None:
    """PTB 开始停止的第一刻调用：关闭准入，给在途轮次宽限，到期后取消。必须在事件循环内调用。"""
    admission = get_admission()
    if admission.closed:
        return
    logger.info(
        "shutdown requested: admission closed, queued turns rejected, %s in flight",
        admission.running,
    )
    admission.begin_shutdown(float(config.RUNTIME_SHUTDOWN_GRACE_SECONDS))


async def _step(name: str, action: Callable[[], Awaitable[object] | object]) -> None:
    try:
        result = action()
        if inspect.isawaitable(result):
            await result
    except Exception:
        logger.exception("shutdown step failed: %s", name)
    else:
        logger.debug("shutdown step done: %s", name)


async def shutdown_runtime() -> None:
    """`post_stop` 里的关停顺序，见模块文档。"""
    admission = get_admission()

    async def stop_intake() -> None:
        admission.close()
        await admission.aclose()
        if admission.running:
            logger.warning("%s conversation turn(s) still running at post_stop", admission.running)

    async def cancel_background() -> None:
        cancelled = await background.BACKGROUND.shutdown()
        if cancelled:
            logger.info("cancelled %s pending background task(s)", cancelled)

    def close_requests_sessions() -> None:
        closed = http_sessions.close_tracked_sessions()
        if closed:
            logger.debug("closed %s requests session(s)", closed)

    await _step("stop intake", stop_intake)
    await _step("cancel background tasks", cancel_background)
    await _step("flush telegram history", flush_all_pending_events)
    await _step("close litellm clients", litellm_client.close_clients)
    await _step("close requests sessions", close_requests_sessions)
    await _step("shut down thread adapters", blocking.shutdown_all)
    await _step("dispose database engine", db.dispose_engine)
