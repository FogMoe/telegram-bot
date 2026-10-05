"""AI 定时任务与空闲跟进共用的 claim 所有权、租约与尝试记录。

契约和各阶段的恢复策略见 docs/job-recovery.md。这里只放两个 job 共用、与业务无关的部分：

- 每次 claim 生成新的随机 token；所有状态转换与完成写入都带 `claim_token = %s`，
  影响行数为 0 就是失去所有权（`ClaimLostError`），调用方必须停止后续副作用。
- 租约 `claim_until` 用数据库时钟（`UTC_TIMESTAMP()`）写入和比较，多进程之间没有时钟偏差；
  `run_leased` 在 worker 执行期间定期续期，续期失败、租约自我确认超时或执行超过上限都会取消 worker。
- `ai_job_attempts`：每次 claim 一行，记录阶段与结果。阶段转换与对应的业务写入在同一个事务里提交。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_telegram_bot.core import sql
from fogmoe_telegram_bot.core.redaction import describe_exception

logger = logging.getLogger(__name__)

# 租约 5 分钟，每分钟续期一次：进程崩溃后最迟 5 分钟（加一个轮询间隔）被回收。
DEFAULT_LEASE_SECONDS = 300
DEFAULT_HEARTBEAT_SECONDS = 60
# 单次执行的硬上限：卡死的线程或工具不能无限期占着 claim。
DEFAULT_EXECUTION_TIMEOUT_SECONDS = 1800
# 每次 claim 的尝试记录保留天数，超过后由轮询顺带清理。
ATTEMPT_RETENTION_DAYS = 30
ATTEMPT_PRUNE_BATCH = 200
ERROR_TEXT_LIMIT = 500

# 阶段。`idle` 表示没有 claim；其余按先后顺序推进，只会向前。
STAGE_IDLE = "idle"
STAGE_CLAIMED = "claimed"  # 只做了只读检查，没有外部副作用：回收后可以安全重跑
STAGE_GENERATING = "generating"  # 模型与工具可能已经产生副作用：不重跑整轮
STAGE_DELIVERING = "delivering"  # Telegram 投递中：结果未知，不重发
STAGE_COMPLETED = "completed"  # 只出现在尝试记录里

# 尝试记录的结果。
OUTCOME_COMPLETED = "completed"  # 本地已确认完成
OUTCOME_FAILED = "failed"  # 已知失败，不再重试
OUTCOME_RETRY = "retry"  # 已知失败且没有副作用，放回队列重试
OUTCOME_PAUSED = "paused"  # 余额或每日上限不满足，放回队列
OUTCOME_RELEASED = "released"  # 进程停止，尚未开始就释放
OUTCOME_EXPIRED = "expired"  # 租约在 claimed 阶段到期，放回队列重跑
OUTCOME_UNKNOWN = "unknown"  # 租约在 generating/delivering 阶段到期，结果未知，不重跑
OUTCOME_ABANDONED = "abandoned"  # claimed 阶段反复到期，超过重试上限
OUTCOME_SUPERSEDED = "superseded"  # claim 已经不在任何任务行上（被改写、替换或用户有了新活动）


class ClaimLostError(Exception):
    """claim 的 token 与数据库不一致：已被回收、替换，或用户有了新活动。

    收到它的 worker 必须停止后续副作用，也不能再写这个任务的状态。
    """


@dataclass(frozen=True)
class JobKind:
    job_type: str
    table: str
    key_column: str


SCHEDULE_JOB = JobKind("schedule", "ai_schedules", "id")
IDLE_FOLLOWUP_JOB = JobKind("idle_followup", "ai_idle_followups", "user_id")


def new_claim_token() -> str:
    """128 位随机数的 32 位十六进制串，每次 claim 都不同。"""
    return uuid.uuid4().hex


def truncate_error(text: str, limit: int = ERROR_TEXT_LIMIT) -> str:
    return text if len(text) <= limit else text[: max(limit - 1, 0)] + "…"


async def open_attempt(
    connection: AsyncConnection,
    kind: JobKind,
    job_id: int,
    token: str,
    attempt_no: int,
    *,
    job_version: int | None = None,
) -> None:
    """记录一次 claim；与 claim 本身在同一个事务里调用。"""
    await connection.exec_driver_sql(
        "INSERT INTO ai_job_attempts "
        "(job_type, job_id, job_version, claim_token, attempt_no, stage, claimed_at, stage_at) "
        "VALUES (%s, %s, %s, %s, %s, 'claimed', UTC_TIMESTAMP(), UTC_TIMESTAMP())",
        (kind.job_type, job_id, job_version, token, attempt_no),
    )


async def advance_stage(
    connection: AsyncConnection,
    kind: JobKind,
    job_id: int,
    token: str,
    stage: str,
    *,
    extra_where: str = "",
    extra_params: tuple[Any, ...] = (),
) -> None:
    """把任务推进到 `stage`，同时更新尝试记录；token 不符或租约已过期则抛 ClaimLostError。

    要求租约仍然有效：租约都确认不了的 worker 不应该开始产生副作用。
    调用方把它和同一阶段的其他数据库写入放进同一个事务。
    """
    result = await connection.exec_driver_sql(
        f"UPDATE {kind.table} SET stage = %s "
        f"WHERE {kind.key_column} = %s AND claim_token = %s AND status = 'executing' "
        f"AND claim_until > UTC_TIMESTAMP(){extra_where}",
        (stage, job_id, token, *extra_params),
    )
    if result.rowcount != 1:
        raise ClaimLostError(f"{kind.job_type} {job_id} is no longer owned by this claim")
    await connection.exec_driver_sql(
        "UPDATE ai_job_attempts SET stage = %s, stage_at = UTC_TIMESTAMP() "
        "WHERE claim_token = %s AND outcome IS NULL",
        (stage, token),
    )


async def close_attempt(
    connection: AsyncConnection,
    token: str,
    outcome: str,
    *,
    stage: str | None = None,
    error: str | None = None,
) -> None:
    """给尝试记录写上结果；已经有结果的记录不会被覆盖。"""
    await connection.exec_driver_sql(
        "UPDATE ai_job_attempts SET outcome = %s, stage = COALESCE(%s, stage), error = %s, "
        "stage_at = UTC_TIMESTAMP(), finished_at = UTC_TIMESTAMP() "
        "WHERE claim_token = %s AND outcome IS NULL",
        (outcome, stage, None if error is None else truncate_error(error), token),
    )


async def renew_lease(
    kind: JobKind,
    job_id: int,
    token: str,
    lease_seconds: float,
    *,
    extra_where: str = "",
    extra_params: tuple[Any, ...] = (),
) -> bool:
    """延长租约；返回 False 说明 claim 已不属于这个 worker。"""
    updated = await sql.execute(
        f"UPDATE {kind.table} "
        "SET claim_until = DATE_ADD(UTC_TIMESTAMP(), INTERVAL %s SECOND) "
        f"WHERE {kind.key_column} = %s AND claim_token = %s AND status = 'executing'"
        f"{extra_where}",
        (int(lease_seconds), job_id, token, *extra_params),
    )
    return updated == 1


async def sweep_orphaned_attempts(kind: JobKind) -> int:
    """关闭不再挂在任何任务行上的未完成尝试（任务行被改写、替换，或用户有了新活动）。"""
    return await sql.execute(
        "UPDATE ai_job_attempts AS a "
        "SET a.outcome = %s, a.finished_at = UTC_TIMESTAMP(), a.stage_at = UTC_TIMESTAMP() "
        "WHERE a.job_type = %s AND a.outcome IS NULL AND NOT EXISTS ("
        f"SELECT 1 FROM {kind.table} AS j "
        f"WHERE j.{kind.key_column} = a.job_id AND j.claim_token = a.claim_token)",
        (OUTCOME_SUPERSEDED, kind.job_type),
    )


async def prune_attempts() -> int:
    return await sql.execute(
        "DELETE FROM ai_job_attempts WHERE outcome IS NOT NULL "
        "AND finished_at < DATE_SUB(UTC_TIMESTAMP(), INTERVAL %s DAY) LIMIT %s",
        (ATTEMPT_RETENTION_DAYS, ATTEMPT_PRUNE_BATCH),
    )


def error_summary(exc: BaseException) -> str:
    return describe_exception(exc, limit=ERROR_TEXT_LIMIT)


async def _heartbeat(
    renew: Callable[[], Awaitable[bool]],
    lease_seconds: float,
    heartbeat_seconds: float,
    label: str,
) -> None:
    """周期性续期；只在失去所有权时返回。

    续期遇到数据库异常时只记日志并等下一拍；但距离上次成功确认已经满一个租约，
    别的进程可能已经回收了这个 claim，worker 按失去所有权处理。
    """
    last_confirmed = time.monotonic()
    while True:
        await asyncio.sleep(heartbeat_seconds)
        try:
            owned = await renew()
        except Exception as exc:
            logger.warning("%s lease renewal failed: %s", label, describe_exception(exc))
            if time.monotonic() - last_confirmed >= lease_seconds:
                logger.error("%s lease could not be confirmed within %ss", label, lease_seconds)
                return
            continue
        if not owned:
            logger.warning("%s lease renewal rejected: the claim changed hands", label)
            return
        last_confirmed = time.monotonic()


async def run_leased[T](
    work: Awaitable[T],
    *,
    renew: Callable[[], Awaitable[bool]],
    lease_seconds: float,
    heartbeat_seconds: float,
    timeout: float | None,
    abort_event: threading.Event | None = None,
    label: str = "job",
) -> T:
    """在后台续期的同时运行 `work`。

    - 续期被拒绝（token 已变）或租约无法确认：取消 worker，抛 `ClaimLostError`。
    - 超过 `timeout`：取消 worker，抛 `TimeoutError`；此时 claim 仍属于本 worker，由调用方收尾。
    - `abort_event` 在取消的同时置位，让线程池里的工具循环在下一个检查点退出。
    """
    work_task: asyncio.Future[T] = asyncio.ensure_future(work)
    heartbeat_task = asyncio.create_task(
        _heartbeat(renew, lease_seconds, heartbeat_seconds, label)
    )
    try:
        racers: set[asyncio.Future[Any]] = {work_task, heartbeat_task}
        done, _ = await asyncio.wait(
            racers,
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if work_task in done:
            return work_task.result()
        if abort_event is not None:
            abort_event.set()
        if heartbeat_task in done:
            raise ClaimLostError(f"{label} lost its lease")
        raise TimeoutError(f"{label} exceeded the {timeout:g}s execution limit")
    finally:
        if abort_event is not None and not work_task.done():
            abort_event.set()
        for task in (work_task, heartbeat_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(work_task, heartbeat_task, return_exceptions=True)


__all__ = [
    "ATTEMPT_RETENTION_DAYS",
    "ClaimLostError",
    "DEFAULT_EXECUTION_TIMEOUT_SECONDS",
    "DEFAULT_HEARTBEAT_SECONDS",
    "DEFAULT_LEASE_SECONDS",
    "IDLE_FOLLOWUP_JOB",
    "JobKind",
    "SCHEDULE_JOB",
    "advance_stage",
    "close_attempt",
    "error_summary",
    "new_claim_token",
    "open_attempt",
    "prune_attempts",
    "renew_lease",
    "run_leased",
    "sweep_orphaned_attempts",
]
