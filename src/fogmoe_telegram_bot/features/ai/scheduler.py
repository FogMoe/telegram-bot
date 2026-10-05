"""AI 定时任务轮询。

claim 的所有权、租约、阶段与各阶段的恢复策略见 docs/job-recovery.md。要点：

- 一次只 claim 一个任务：崩溃最多卡住当前这一个，其余仍是 pending。
- 每次 claim 带新的随机 token；之后所有状态写入都以 `claim_token` 为条件，影响行数为 0 就停手。
- 租约由后台心跳续期；进程崩溃后租约到期，下一次轮询按阶段回收：
  claimed 阶段安全重跑，generating/delivering 阶段结果未知、不重跑，循环任务只推进一次。
"""

import asyncio
import logging
import threading
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from telegram.ext import ContextTypes

from fogmoe_telegram_bot.core import mysql_connection, process_user
from fogmoe_telegram_bot.core.archive_utils import send_permanent_records_archive
from fogmoe_telegram_bot.core.prompt_utils import format_metadata_attrs, xml_escape
from fogmoe_telegram_bot.core.telegram_history import suppress_telegram_history, telegram_history_scope
from fogmoe_telegram_bot.core.telegram_utils import partial_send
from fogmoe_telegram_bot.features.ai import ai_chat, job_claims, summary
from fogmoe_telegram_bot.features.ai.conversation_locks import get_conversation_lock
from fogmoe_telegram_bot.features.ai.job_claims import ClaimLostError
from fogmoe_telegram_bot.features.ai.outbound import send_generated_media
from fogmoe_telegram_bot.features.ai.reply_filter import normalize_ai_reply_text
from fogmoe_telegram_bot.features.ai.schedule_limits import (
    DAILY_SCHEDULE_TRIGGER_LIMIT,
    reserve_daily_schedule_trigger,
)
from fogmoe_telegram_bot.features.ai.sticker_sender import normalize_sticker_directives, send_ai_reply_with_stickers
from fogmoe_telegram_bot.features.ai.telegram_visible_sender import TelegramVisibleContentHandler
from fogmoe_telegram_bot.features.ai.tool_history import tool_logs_to_record_entries
from fogmoe_telegram_bot.features.ai.types import ABORT_EVENT_KEY
from fogmoe_telegram_bot.features.ai.user_state import build_user_state_prompt

logger = logging.getLogger(__name__)

SCHEDULE_POLL_INTERVAL = 60
# 每次轮询最多依次处理的任务数；每个任务单独 claim。
SCHEDULE_BATCH_SIZE = 5
SCHEDULE_LEASE_SECONDS = job_claims.DEFAULT_LEASE_SECONDS
SCHEDULE_HEARTBEAT_SECONDS = job_claims.DEFAULT_HEARTBEAT_SECONDS
SCHEDULE_EXECUTION_TIMEOUT_SECONDS = job_claims.DEFAULT_EXECUTION_TIMEOUT_SECONDS
# 同一次运行最多被 claim 几次：claimed 阶段反复崩溃的任务不会无限重试。
SCHEDULE_MAX_CLAIM_ATTEMPTS = 3
RECOVERY_BATCH_SIZE = 50

INTERRUPTED_ERROR = (
    "Interrupted during {stage}; the outcome is unknown, so the task was not retried "
    "to avoid duplicate delivery"
)
ABANDONED_ERROR = "Abandoned after {attempts} claims that never started running"

# 释放 claim 时 claim_attempts 的处理。
_ATTEMPTS_RESET = "0"  # 暂停（余额或每日上限）：这次运行重新开始
_ATTEMPTS_KEEP = "claim_attempts"  # 已知失败后重试：计入上限
_ATTEMPTS_REFUND = "GREATEST(claim_attempts - 1, 0)"  # 关停时尚未开始：不算一次尝试

_schedule_lock = asyncio.Lock()


@dataclass(frozen=True)
class ScheduleClaim:
    """一次 claim 所持有的任务快照与所有权凭证。"""

    schedule_id: int
    user_id: int
    run_at: datetime
    created_at: Optional[datetime]
    trigger_reason: str
    context_text: Optional[str]
    instruction: str
    recurrence_unit: str
    recurrence_interval: int
    token: str
    attempt: int


class _ScheduleRun:
    """一次 claim 的运行状态：已经落库的最新阶段，以及撤销信号。"""

    def __init__(self, claim: ScheduleClaim) -> None:
        self.claim = claim
        self.stage = job_claims.STAGE_CLAIMED
        self.abort_event = threading.Event()


def _text(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    return value


def _recurrence_delta(unit: str, interval: int) -> Optional[timedelta]:
    if unit == "minute":
        return timedelta(minutes=interval)
    if unit == "hour":
        return timedelta(hours=interval)
    if unit == "day":
        return timedelta(days=interval)
    return None


def _calculate_next_run_at(
    previous_run_at: datetime,
    recurrence_unit: str,
    recurrence_interval: int,
) -> Optional[datetime]:
    delta = _recurrence_delta(recurrence_unit, recurrence_interval)
    if delta is None:
        return None

    if previous_run_at.tzinfo is not None:
        previous_run_at = previous_run_at.astimezone(timezone.utc).replace(tzinfo=None)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    next_run_at = previous_run_at + delta
    while next_run_at <= now:
        next_run_at += delta
    return next_run_at


def _format_timestamp(value: Optional[datetime]) -> str:
    if not value:
        return ""
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _format_scheduled_message(
    *,
    timestamp: datetime,
    scheduled_at: Optional[datetime],
    scheduled_for: Optional[datetime],
    trigger_reason: str,
    context_text: Optional[str],
    instruction: str,
) -> str:
    attrs = [
        ("type", "scheduler"),
        ("timestamp", _format_timestamp(timestamp)),
        ("origin", "scheduled_task"),
    ]
    if scheduled_at:
        attrs.append(("scheduled_at", _format_timestamp(scheduled_at)))
    if scheduled_for:
        attrs.append(("scheduled_for", _format_timestamp(scheduled_for)))

    attr_text = format_metadata_attrs(attrs)
    lines = [f"<metadata {attr_text}>"]
    lines.append(f"  <trigger>{xml_escape(trigger_reason)}</trigger>")
    if context_text:
        lines.append(f"  <context>{xml_escape(context_text)}</context>")
    lines.append(f"  <instruction>{xml_escape(instruction)}</instruction>")
    lines.append("</metadata>")
    return "\n".join(lines)


async def _handle_overflow_summary(conversation_id: int, level: Optional[str]) -> None:
    if level != "overflow":
        return
    summary_text = await summary.generate_summary_immediately(conversation_id)
    if summary_text:
        await mysql_connection.async_update_latest_history_state_summary(
            conversation_id,
            summary_text,
        )
    else:
        summary.schedule_summary_generation(conversation_id)


async def _persist_tool_logs(
    conversation_id: int,
    tool_logs: list[dict],
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
) -> None:
    tool_record_entries = tool_logs_to_record_entries(tool_logs)

    if tool_record_entries:
        snapshot_created, warning_level, archived_records = await mysql_connection.async_insert_chat_records(
            conversation_id,
            tool_record_entries,
        )
        if archived_records:
            await send_permanent_records_archive(
                context.bot,
                user_id,
                archived_records,
                logger=logger,
            )
        await _handle_overflow_summary(conversation_id, warning_level)
        if snapshot_created and warning_level != "overflow":
            summary.schedule_summary_generation(conversation_id)


def _claim_from_row(row, token: str, attempt: int) -> ScheduleClaim:
    recurrence_unit = (_text(row[7]) or "none").strip().lower()
    try:
        recurrence_interval = int(row[8] or 1)
    except (TypeError, ValueError):
        recurrence_interval = 1
    return ScheduleClaim(
        schedule_id=int(row[0]),
        user_id=int(row[1]),
        run_at=row[2],
        created_at=row[3],
        trigger_reason=_text(row[4]) or "",
        context_text=_text(row[5]) or "",
        instruction=_text(row[6]) or "",
        recurrence_unit=recurrence_unit,
        recurrence_interval=max(recurrence_interval, 1),
        token=token,
        attempt=attempt,
    )


async def _claim_next_schedule(
    exclude_ids: Collection[int] = (),
) -> Optional[ScheduleClaim]:
    """claim 一个到期任务：新 token、新租约、同一事务里写下尝试记录。

    一次只 claim 一个，崩溃最多卡住当前这一个；SKIP LOCKED 让多个进程互不阻塞。
    `exclude_ids` 是本轮已经处理过的任务：失败后放回队列的任务留给下一次轮询，不在本轮反复重试。
    """
    token = job_claims.new_claim_token()
    exclusion = ""
    params: list = [DAILY_SCHEDULE_TRIGGER_LIMIT]
    if exclude_ids:
        exclusion = f"AND s.id NOT IN ({', '.join(['%s'] * len(exclude_ids))}) "
        params.extend(exclude_ids)
    async with mysql_connection.transaction() as connection:
        rows = await mysql_connection.fetch_all(
            "SELECT s.id, s.user_id, s.run_at, s.created_at, s.trigger_reason, "
            "s.context, s.prompt, s.recurrence_unit, s.recurrence_interval, "
            "s.claim_attempts "
            "FROM ai_schedules AS s "
            "LEFT JOIN user AS u ON u.id = s.user_id "
            "WHERE s.status = 'pending' AND s.run_at <= UTC_TIMESTAMP() "
            "AND (u.id IS NULL OR "
            "COALESCE(u.coins, 0) + COALESCE(u.coins_paid, 0) > 0) "
            "AND (u.id IS NULL OR u.ai_schedule_trigger_date IS NULL "
            "OR u.ai_schedule_trigger_date <> UTC_DATE() "
            "OR u.ai_schedule_trigger_count < %s) "
            f"{exclusion}"
            "ORDER BY s.run_at ASC, s.id ASC "
            "LIMIT 1 FOR UPDATE OF s SKIP LOCKED",
            tuple(params),
            connection=connection,
        )
        if not rows:
            return None

        row = rows[0]
        attempt = int(row[9] or 0) + 1
        result = await connection.exec_driver_sql(
            "UPDATE ai_schedules "
            "SET status = 'executing', stage = 'claimed', claim_token = %s, "
            "claim_until = DATE_ADD(UTC_TIMESTAMP(), INTERVAL %s SECOND), "
            "claim_attempts = %s "
            "WHERE id = %s AND status = 'pending'",
            (token, int(SCHEDULE_LEASE_SECONDS), attempt, row[0]),
        )
        if result.rowcount != 1:
            return None
        await job_claims.open_attempt(
            connection,
            job_claims.SCHEDULE_JOB,
            int(row[0]),
            token,
            attempt,
        )
    return _claim_from_row(row, token, attempt)


async def _settle_schedule(
    connection,
    *,
    schedule_id: int,
    token: Optional[str],
    run_at: datetime,
    recurrence_unit: str,
    recurrence_interval: int,
    error: Optional[str],
    outcome: str,
    advance_recurring: bool = True,
) -> None:
    """终结一次 claim，状态写入和尝试记录在同一个事务里。

    一次性任务落到 executed/failed。循环任务无论本次结果如何，下一次运行时间只推进一次，
    失败原因保留在 error 里；`advance_recurring=False`（用户不存在等永久性失败）直接 failed。
    token 不符（已被回收或替换）抛 ClaimLostError，什么都不会写。
    """
    next_run_at = (
        _calculate_next_run_at(run_at, recurrence_unit, recurrence_interval)
        if advance_recurring
        else None
    )
    clear_claim = (
        "stage = 'idle', claim_token = NULL, claim_until = NULL, claim_attempts = 0 "
        "WHERE id = %s AND claim_token <=> %s AND status = 'executing'"
    )
    if next_run_at is None and error is None:
        statement = (
            "UPDATE ai_schedules SET status = 'executed', executed_at = UTC_TIMESTAMP(), "
            f"error = NULL, {clear_claim}"
        )
        params: tuple = (schedule_id, token)
    elif next_run_at is None:
        statement = f"UPDATE ai_schedules SET status = 'failed', error = %s, {clear_claim}"
        params = (error, schedule_id, token)
    elif error is None:
        statement = (
            "UPDATE ai_schedules SET status = 'pending', run_at = %s, last_run_at = %s, "
            f"executed_at = UTC_TIMESTAMP(), error = NULL, {clear_claim}"
        )
        params = (next_run_at, run_at, schedule_id, token)
    else:
        statement = (
            "UPDATE ai_schedules SET status = 'pending', run_at = %s, last_run_at = %s, "
            f"error = %s, {clear_claim}"
        )
        params = (next_run_at, run_at, error, schedule_id, token)

    result = await connection.exec_driver_sql(statement, params)
    if result.rowcount != 1:
        raise ClaimLostError(f"schedule {schedule_id} is no longer owned by this claim")
    if token:
        await job_claims.close_attempt(
            connection,
            token,
            outcome,
            stage=job_claims.STAGE_COMPLETED if outcome == job_claims.OUTCOME_COMPLETED else None,
            error=error,
        )


async def _release_in_txn(
    connection,
    claim: ScheduleClaim,
    outcome: str,
    *,
    attempts_expr: str,
    error: Optional[str] = None,
) -> None:
    result = await connection.exec_driver_sql(
        "UPDATE ai_schedules SET status = 'pending', stage = 'idle', "
        "claim_token = NULL, claim_until = NULL, "
        f"claim_attempts = {attempts_expr}, error = COALESCE(%s, error) "
        "WHERE id = %s AND claim_token = %s AND status = 'executing'",
        (error, claim.schedule_id, claim.token),
    )
    if result.rowcount != 1:
        raise ClaimLostError(f"schedule {claim.schedule_id} is no longer owned by this claim")
    await job_claims.close_attempt(connection, claim.token, outcome, error=error)


async def _release_claim(
    run: _ScheduleRun,
    outcome: str,
    *,
    attempts_expr: str,
    error: Optional[str] = None,
) -> None:
    """把还没产生副作用的 claim 放回 pending。"""
    async with mysql_connection.transaction() as connection:
        await _release_in_txn(
            connection,
            run.claim,
            outcome,
            attempts_expr=attempts_expr,
            error=error,
        )


async def _begin_generation(run: _ScheduleRun) -> bool:
    """claimed -> generating：占用每日额度与阶段转换在同一个事务里。

    返回 False 表示每日额度已满，任务已放回 pending。这一步之前没有任何外部副作用，
    之后模型与工具可能已经产生副作用，崩溃后不再重跑。
    """
    claim = run.claim
    async with mysql_connection.transaction() as connection:
        owned = await mysql_connection.fetch_one(
            "SELECT 1 FROM ai_schedules "
            "WHERE id = %s AND claim_token = %s AND status = 'executing' "
            "AND claim_until > UTC_TIMESTAMP() FOR UPDATE",
            (claim.schedule_id, claim.token),
            connection=connection,
        )
        if not owned:
            raise ClaimLostError(f"schedule {claim.schedule_id} is no longer owned by this claim")
        if not await reserve_daily_schedule_trigger(claim.user_id, connection=connection):
            await _release_in_txn(
                connection,
                claim,
                job_claims.OUTCOME_PAUSED,
                attempts_expr=_ATTEMPTS_RESET,
            )
            return False
        await job_claims.advance_stage(
            connection,
            job_claims.SCHEDULE_JOB,
            claim.schedule_id,
            claim.token,
            job_claims.STAGE_GENERATING,
        )
        await connection.exec_driver_sql(
            "UPDATE ai_job_attempts SET daily_trigger_reserved = 1 WHERE claim_token = %s",
            (claim.token,),
        )
    run.stage = job_claims.STAGE_GENERATING
    return True


async def _begin_delivery(run: _ScheduleRun) -> None:
    """generating -> delivering：从这里开始，崩溃后的投递结果未知，不会重发。"""
    claim = run.claim
    async with mysql_connection.transaction() as connection:
        await job_claims.advance_stage(
            connection,
            job_claims.SCHEDULE_JOB,
            claim.schedule_id,
            claim.token,
            job_claims.STAGE_DELIVERING,
        )
    run.stage = job_claims.STAGE_DELIVERING


async def _complete_claim(run: _ScheduleRun) -> None:
    claim = run.claim
    async with mysql_connection.transaction() as connection:
        await _settle_schedule(
            connection,
            schedule_id=claim.schedule_id,
            token=claim.token,
            run_at=claim.run_at,
            recurrence_unit=claim.recurrence_unit,
            recurrence_interval=claim.recurrence_interval,
            error=None,
            outcome=job_claims.OUTCOME_COMPLETED,
        )


async def _record_failure(run: _ScheduleRun, exc: BaseException) -> None:
    """已知失败的收尾，按失败发生的阶段决定能否重试。"""
    claim = run.claim
    error_text = job_claims.error_summary(exc)
    try:
        async with mysql_connection.transaction() as connection:
            if (
                run.stage == job_claims.STAGE_CLAIMED
                and claim.attempt < SCHEDULE_MAX_CLAIM_ATTEMPTS
            ):
                # 还没有任何副作用：放回队列，下一次轮询重新 claim。
                await _release_in_txn(
                    connection,
                    claim,
                    job_claims.OUTCOME_RETRY,
                    attempts_expr=_ATTEMPTS_KEEP,
                    error=error_text,
                )
            else:
                await _settle_schedule(
                    connection,
                    schedule_id=claim.schedule_id,
                    token=claim.token,
                    run_at=claim.run_at,
                    recurrence_unit=claim.recurrence_unit,
                    recurrence_interval=claim.recurrence_interval,
                    error=error_text,
                    outcome=job_claims.OUTCOME_FAILED,
                )
    except ClaimLostError:
        logger.warning(
            "Scheduled task %s lost its claim before the failure could be recorded",
            claim.schedule_id,
        )
    except Exception:
        # 写不进去也没关系：租约到期后按所处阶段回收。
        logger.exception(
            "Failed to record scheduled task failure: schedule_id=%s", claim.schedule_id
        )


async def _recover_expired_schedules() -> int:
    """回收租约已到期的 executing 任务（崩溃、卡死或被撤销的 worker 留下的）。

    - claimed 阶段：没有副作用，放回 pending 重新 claim；反复到期超过上限则放弃。
    - generating / delivering 阶段：外部结果未知，不重跑整轮、不重发；
      一次性任务记为 failed，循环任务推进一次下一次运行时间。
    """
    recovered = 0
    async with mysql_connection.transaction() as connection:
        rows = await mysql_connection.fetch_all(
            "SELECT id, run_at, recurrence_unit, recurrence_interval, stage, "
            "claim_token, claim_attempts "
            "FROM ai_schedules "
            "WHERE status = 'executing' AND claim_until <= UTC_TIMESTAMP() "
            "ORDER BY claim_until ASC, id ASC LIMIT %s FOR UPDATE SKIP LOCKED",
            (RECOVERY_BATCH_SIZE,),
            connection=connection,
        )
        for (
            schedule_id,
            run_at,
            recurrence_unit,
            recurrence_interval,
            stage,
            token,
            attempts,
        ) in rows:
            attempts = int(attempts or 0)
            if stage == job_claims.STAGE_CLAIMED and attempts < SCHEDULE_MAX_CLAIM_ATTEMPTS:
                result = await connection.exec_driver_sql(
                    "UPDATE ai_schedules SET status = 'pending', stage = 'idle', "
                    "claim_token = NULL, claim_until = NULL "
                    "WHERE id = %s AND claim_token <=> %s AND status = 'executing'",
                    (schedule_id, token),
                )
                if result.rowcount == 1 and token:
                    await job_claims.close_attempt(
                        connection, token, job_claims.OUTCOME_EXPIRED
                    )
                logger.warning(
                    "Recovered expired scheduled task claim for a safe re-run: "
                    "schedule_id=%s attempt=%s",
                    schedule_id,
                    attempts,
                )
            else:
                if stage == job_claims.STAGE_CLAIMED:
                    outcome = job_claims.OUTCOME_ABANDONED
                    error = ABANDONED_ERROR.format(attempts=attempts)
                else:
                    outcome = job_claims.OUTCOME_UNKNOWN
                    error = INTERRUPTED_ERROR.format(stage=stage)
                await _settle_schedule(
                    connection,
                    schedule_id=int(schedule_id),
                    token=token,
                    run_at=run_at,
                    recurrence_unit=(_text(recurrence_unit) or "none").strip().lower(),
                    recurrence_interval=max(int(recurrence_interval or 1), 1),
                    error=error,
                    outcome=outcome,
                )
                logger.warning(
                    "Scheduled task interrupted; not retried: schedule_id=%s stage=%s outcome=%s",
                    schedule_id,
                    stage,
                    outcome,
                )
            recovered += 1
    return recovered


async def _run_housekeeping() -> None:
    try:
        await _recover_expired_schedules()
        await job_claims.sweep_orphaned_attempts(job_claims.SCHEDULE_JOB)
        await job_claims.prune_attempts()
    except Exception:
        logger.exception("Scheduled task recovery pass failed")


def _application_stopping(context) -> bool:
    """应用已经开始停止：不再 claim 新任务，尚未开始的 claim 直接释放。"""
    application = getattr(context, "application", None)
    return application is not None and not application.running


async def _release_if_unstarted(run: _ScheduleRun) -> None:
    if run.stage != job_claims.STAGE_CLAIMED:
        return
    try:
        await asyncio.shield(
            _release_claim(
                run,
                job_claims.OUTCOME_RELEASED,
                attempts_expr=_ATTEMPTS_REFUND,
            )
        )
    except Exception:
        # 释放不了就等租约到期，claimed 阶段会被安全重跑。
        logger.warning(
            "Could not release scheduled task claim on shutdown: schedule_id=%s",
            run.claim.schedule_id,
        )


async def _process_schedule_task(
    claim: ScheduleClaim,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """在租约保护下处理一个已经 claim 的任务。"""
    run = _ScheduleRun(claim)
    try:
        await job_claims.run_leased(
            _run_schedule_claim(run, context),
            renew=lambda: job_claims.renew_lease(
                job_claims.SCHEDULE_JOB,
                claim.schedule_id,
                claim.token,
                SCHEDULE_LEASE_SECONDS,
            ),
            lease_seconds=SCHEDULE_LEASE_SECONDS,
            heartbeat_seconds=SCHEDULE_HEARTBEAT_SECONDS,
            timeout=SCHEDULE_EXECUTION_TIMEOUT_SECONDS,
            abort_event=run.abort_event,
            label=f"schedule {claim.schedule_id}",
        )
    except ClaimLostError:
        logger.warning(
            "Scheduled task %s no longer owns its claim; stopped without further writes",
            claim.schedule_id,
        )
    except asyncio.CancelledError:
        await _release_if_unstarted(run)
        raise
    except Exception as exc:
        logger.exception("Scheduled task %s failed: %s", claim.schedule_id, exc)
        await _record_failure(run, exc)


async def _run_schedule_claim(
    run: _ScheduleRun,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    async with get_conversation_lock(run.claim.user_id):
        await _process_schedule_task_locked(run, context)


async def _process_schedule_task_locked(
    run: _ScheduleRun,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    claim = run.claim
    user_id = claim.user_id
    schedule_id = claim.schedule_id

    user_state_prompt = await build_user_state_prompt(user_id)
    if user_state_prompt is None:
        async with mysql_connection.transaction() as connection:
            await _settle_schedule(
                connection,
                schedule_id=schedule_id,
                token=claim.token,
                run_at=claim.run_at,
                recurrence_unit=claim.recurrence_unit,
                recurrence_interval=claim.recurrence_interval,
                error="user not found",
                outcome=job_claims.OUTCOME_FAILED,
                advance_recurring=False,
            )
        return

    if await process_user.async_get_user_coins(user_id) < 1:
        await _release_claim(
            run,
            job_claims.OUTCOME_PAUSED,
            attempts_expr=_ATTEMPTS_RESET,
        )
        logger.info(
            "Scheduled task paused until coins are available: user_id=%s schedule_id=%s",
            user_id,
            schedule_id,
        )
        return

    if _application_stopping(context):
        await _release_claim(
            run,
            job_claims.OUTCOME_RELEASED,
            attempts_expr=_ATTEMPTS_REFUND,
        )
        return

    if not await _begin_generation(run):
        logger.info(
            "Scheduled task paused at daily trigger limit: user_id=%s schedule_id=%s limit=%s",
            user_id,
            schedule_id,
            DAILY_SCHEDULE_TRIGGER_LIMIT,
        )
        return

    now_utc = datetime.now(timezone.utc)
    scheduled_message = _format_scheduled_message(
        timestamp=now_utc,
        scheduled_at=claim.created_at,
        scheduled_for=claim.run_at,
        trigger_reason=claim.trigger_reason,
        context_text=claim.context_text,
        instruction=claim.instruction,
    )

    snapshot_created, warning_level, archived_records = await mysql_connection.async_insert_chat_record(
        user_id,
        "user",
        scheduled_message,
        system_prompt_extra=user_state_prompt,
    )
    if archived_records:
        await send_permanent_records_archive(
            context.bot,
            user_id,
            archived_records,
            logger=logger,
        )
    await _handle_overflow_summary(user_id, warning_level)
    if snapshot_created and warning_level != "overflow":
        summary.schedule_summary_generation(user_id)

    chat_history = await mysql_connection.async_get_chat_history(user_id)
    tool_context = {
        "is_group": False,
        "group_id": None,
        "message_id": None,
        "user_id": user_id,
        "user_state_prompt": user_state_prompt,
        ABORT_EVENT_KEY: run.abort_event,
    }

    try:
        await context.bot.send_chat_action(chat_id=user_id, action="typing")
    except Exception:
        logger.debug("Failed to send typing action for scheduled task %s", schedule_id)

    sent_messages: list = []
    send_func = partial_send(context.bot.send_message, user_id)
    visible_content_handler = TelegramVisibleContentHandler(
        bot=context.bot,
        chat_id=user_id,
        first_text_send=send_func,
        fallback_send=send_func,
        logger=logger,
        abort_event=run.abort_event,
    )

    with suppress_telegram_history():
        assistant_message, tool_logs = await ai_chat.get_ai_response(
            list(chat_history),
            user_id,
            tool_context=tool_context,
            visible_content_handler=visible_content_handler,
        )
    sent_messages.extend(visible_content_handler.sent_messages)
    assistant_message = normalize_ai_reply_text(assistant_message)
    runtime_error = ai_chat.runtime_error_cause(assistant_message)
    if assistant_message.strip():
        assistant_message = await normalize_sticker_directives(
            assistant_message,
            logger=logger,
        )

    # 工具结果与回复文本此时只存在于内存里，没有安全的中间状态可以恢复：
    # 进入 delivering 之后崩溃一律按结果未知处理，不重跑、不重发。
    await _begin_delivery(run)

    if tool_logs:
        await _persist_tool_logs(user_id, tool_logs, context, user_id)

    if assistant_message.strip() and not runtime_error:
        snapshot_created, warning_level, archived_records = await mysql_connection.async_insert_chat_record(
            user_id,
            "assistant",
            assistant_message,
        )
        if archived_records:
            await send_permanent_records_archive(
                context.bot,
                user_id,
                archived_records,
                logger=logger,
            )
        await _handle_overflow_summary(user_id, warning_level)
        if snapshot_created and warning_level != "overflow":
            summary.schedule_summary_generation(user_id)

    if assistant_message.strip():
        try:
            await context.bot.send_chat_action(chat_id=user_id, action="typing")
        except Exception:
            logger.debug("Failed to send typing action before scheduled AI reply")
        send_scope = (
            telegram_history_scope(
                user_id=user_id,
                chat_id=user_id,
                chat_type="private",
                origin="bot_runtime",
                event="error_notice",
                cause=runtime_error,
            )
            if runtime_error
            else suppress_telegram_history()
        )
        with send_scope:
            sent_messages.extend(
                await send_ai_reply_with_stickers(
                    bot=context.bot,
                    chat_id=user_id,
                    text=str(assistant_message),
                    first_text_send=send_func,
                    fallback_send=send_func,
                    logger=logger,
                )
            )
    sent_messages.extend(
        await send_generated_media(
            bot=context.bot,
            chat_id=user_id,
            tool_logs=tool_logs,
            logger=logger,
        )
    )
    if not sent_messages and not assistant_message.strip():
        tool_log_types = [
            str(tool_log.get("type", "tool_result"))
            for tool_log in tool_logs
            if isinstance(tool_log, dict)
        ]
        logger.info(
            "Scheduled AI produced empty response; no Telegram message sent: user_id=%s schedule_id=%s tool_log_types=%s",
            user_id,
            schedule_id,
            tool_log_types,
        )
    await _complete_claim(run)


async def run_ai_schedule_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    if _schedule_lock.locked():
        return

    async with _schedule_lock:
        await _run_housekeeping()
        processed: set[int] = set()
        for _ in range(SCHEDULE_BATCH_SIZE):
            if _application_stopping(context):
                break
            claim = await _claim_next_schedule(processed)
            if claim is None:
                break
            processed.add(claim.schedule_id)
            await _process_schedule_task(claim, context)


def setup_schedule_jobs(application) -> None:
    """注册 AI 定时任务轮询。"""

    application.job_queue.run_repeating(
        run_ai_schedule_job,
        interval=SCHEDULE_POLL_INTERVAL,
        first=5,
    )
