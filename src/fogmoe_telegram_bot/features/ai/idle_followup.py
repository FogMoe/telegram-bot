"""Idle private-chat recap and one-shot follow-up handling.

claim 的所有权、租约、阶段与各阶段的恢复策略见 docs/job-recovery.md。要点：

- 每次 claim 带新的随机 token；之后所有状态写入都以 `claim_token` 与 `activity_version`
  为条件，用户的新活动（version 变化）和回收（token 变化）都会让旧 worker 的写入被拒绝。
- 回顾生成只用只读工具，属于 claimed 阶段，崩溃后可以安全重跑；
  主模型带完整工具集，进入 generating 之后崩溃不再重跑整轮，投递阶段结果未知也不重发。
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from statistics import median
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from telegram.ext import ContextTypes

from fogmoe_telegram_bot.core import config, mysql_connection, process_user
from fogmoe_telegram_bot.core.archive_utils import send_permanent_records_archive
from fogmoe_telegram_bot.core.prompt_utils import format_metadata_attrs, xml_escape
from fogmoe_telegram_bot.core.telegram_history import suppress_telegram_history
from fogmoe_telegram_bot.core.telegram_utils import partial_send
from fogmoe_telegram_bot.features.ai import ai_chat, job_claims, summary
from fogmoe_telegram_bot.features.ai.conversation_locks import get_conversation_lock
from fogmoe_telegram_bot.features.ai.job_claims import ClaimLostError
from fogmoe_telegram_bot.features.ai.outbound import send_generated_media
from fogmoe_telegram_bot.features.ai.provider_resolver import (
    completion_kwargs_for_task,
    get_models_for_task,
    get_provider_order_for_task,
)
from fogmoe_telegram_bot.features.ai.reply_filter import normalize_ai_reply_text
from fogmoe_telegram_bot.features.ai.router import runtime_error_cause
from fogmoe_telegram_bot.features.ai.sticker_sender import (
    PartialAIReplySendError,
    normalize_sticker_directives,
    send_ai_reply_with_stickers,
)
from fogmoe_telegram_bot.features.ai.tool_history import tool_logs_to_record_entries
from fogmoe_telegram_bot.features.ai.tool_runner import run_tool_loop
from fogmoe_telegram_bot.features.ai.tools import (
    AI_TOOL_HANDLERS,
    OPENAI_TOOLS,
    clear_tool_request_context,
    set_tool_request_context,
)
from fogmoe_telegram_bot.features.ai.tools.memory_tools import read_diary_page_tool
from fogmoe_telegram_bot.features.ai.tools.schemas import IDLE_RECAP_READ_DIARY_TOOL
from fogmoe_telegram_bot.features.ai.types import ABORT_EVENT_KEY
from fogmoe_telegram_bot.features.ai.user_state import build_user_state_prompt

logger = logging.getLogger(__name__)

IDLE_FOLLOWUP_POLL_INTERVAL = 60
IDLE_FOLLOWUP_BATCH_SIZE = 3
IDLE_FOLLOWUP_SAMPLE_SIZE = 5
IDLE_FOLLOWUP_ENABLED = True
IDLE_FOLLOWUP_DEFAULT_MINUTES = 10
IDLE_FOLLOWUP_MIN_MINUTES = 2
IDLE_FOLLOWUP_MAX_MINUTES = 60
IDLE_FOLLOWUP_LEASE_SECONDS = job_claims.DEFAULT_LEASE_SECONDS
IDLE_FOLLOWUP_HEARTBEAT_SECONDS = job_claims.DEFAULT_HEARTBEAT_SECONDS
IDLE_FOLLOWUP_EXECUTION_TIMEOUT_SECONDS = job_claims.DEFAULT_EXECUTION_TIMEOUT_SECONDS
# 同一次跟进最多被 claim 几次：claimed 阶段反复崩溃的跟进不会无限重试。
# 已知失败的重试（IDLE_FOLLOWUP_MAX_RETRIES）也各占一次 claim，所以上限要比它大。
IDLE_FOLLOWUP_MAX_CLAIM_ATTEMPTS = 5
RECOVERY_BATCH_SIZE = 50
IDLE_FOLLOWUP_RETRY_MINUTES = 15
IDLE_FOLLOWUP_MAX_RETRIES = 3
IDLE_RECAP_MAX_DIALOGUE_MESSAGES = 20
IDLE_RECAP_RETRY_LIMIT = 2
IDLE_RECAP_TIMEOUT_SECONDS = 120
IDLE_RECAP_TOOL_NAMES = frozenset(
    {"fetch_permanent_summaries", "search_permanent_records", "read_diary_page"}
)
# read_diary_page is deliberately absent from OPENAI_TOOLS. Supplying its schema
# and handler only to this loop keeps the facade exclusive to the recap agent.
IDLE_RECAP_TOOLS = [
    tool
    for tool in [*OPENAI_TOOLS, IDLE_RECAP_READ_DIARY_TOOL]
    if (tool.get("function") or {}).get("name") in IDLE_RECAP_TOOL_NAMES
]
IDLE_RECAP_TOOL_HANDLERS = {
    "fetch_permanent_summaries": AI_TOOL_HANDLERS["fetch_permanent_summaries"],
    "search_permanent_records": AI_TOOL_HANDLERS["search_permanent_records"],
    "read_diary_page": read_diary_page_tool,
}

INTERRUPTED_ERROR = (
    "Interrupted during {stage}; the outcome is unknown, so the follow-up was not retried "
    "to avoid repeating tool effects or duplicate delivery"
)
ABANDONED_ERROR = "Abandoned after {attempts} claims that never started running"

# 释放 claim 时 claim_attempts 的处理。
_ATTEMPTS_RESET = "0"  # 暂停（余额不足）：这次跟进重新开始
_ATTEMPTS_REFUND = "GREATEST(claim_attempts - 1, 0)"  # 关停时尚未开始：不算一次尝试

_idle_followup_job_lock = asyncio.Lock()
_MESSAGE_TAG_RE = re.compile(r"<message>(.*?)</message>", re.DOTALL)
_MEDIA_DESCRIPTION_RE = re.compile(r"<description>(.*?)</description>", re.DOTALL)


class IdleRecapMemorySuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    impression: str = Field(max_length=2000)
    diary: str = Field(max_length=2000)


class IdleRecapOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recap: str = Field(max_length=2000)
    open_loops: str = Field(max_length=2000)
    suggested_follow_up: str = Field(max_length=2000)
    memory_suggestion: IdleRecapMemorySuggestion


@dataclass(frozen=True)
class IdleFollowupClaim:
    """一次 claim 的所有权凭证：token 每次 claim 都不同，version 随用户活动变化。"""

    user_id: int
    activity_version: int
    retry_count: int
    token: str = ""
    attempt: int = 1


class _IdleRun:
    """一次 claim 的运行状态：已经落库的最新阶段，以及撤销信号。"""

    def __init__(self, claim: IdleFollowupClaim) -> None:
        self.claim = claim
        self.stage = job_claims.STAGE_CLAIMED
        self.abort_event = threading.Event()


class _NoSideEffectsError(RuntimeError):
    """主模型失败且没有执行过任何工具：可以安全地重试。"""


def calculate_ttl_seconds(
    intervals: Iterable[int | float],
    *,
    default_minutes: int | None = None,
    minimum_minutes: int | None = None,
    maximum_minutes: int | None = None,
) -> int:
    """Return a median-based TTL clamped to the configured bounds."""

    configured_minimum = int(
        minimum_minutes if minimum_minutes is not None else IDLE_FOLLOWUP_MIN_MINUTES
    )
    configured_maximum = int(
        maximum_minutes if maximum_minutes is not None else IDLE_FOLLOWUP_MAX_MINUTES
    )
    lower_minutes = min(configured_minimum, configured_maximum)
    upper_minutes = max(configured_minimum, configured_maximum)
    lower_seconds = max(60, lower_minutes * 60)
    upper_seconds = max(lower_seconds, upper_minutes * 60)

    samples: list[int] = []
    for value in intervals:
        try:
            seconds = int(value)
        except (TypeError, ValueError):
            continue
        if seconds > 0:
            samples.append(seconds)

    if samples:
        ttl_seconds = int(median(samples))
    else:
        fallback_minutes = int(
            default_minutes
            if default_minutes is not None
            else IDLE_FOLLOWUP_DEFAULT_MINUTES
        )
        ttl_seconds = fallback_minutes * 60

    return max(lower_seconds, min(ttl_seconds, upper_seconds))


def _decode_recent_intervals(value: Any) -> list[int]:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="ignore")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
    if not isinstance(value, list):
        return []

    intervals: list[int] = []
    for item in value:
        try:
            seconds = int(item)
        except (TypeError, ValueError):
            continue
        if seconds > 0:
            intervals.append(seconds)
    return intervals[-IDLE_FOLLOWUP_SAMPLE_SIZE:]


def _extract_recent_dialogue(messages: list[dict]) -> list[dict[str, str]]:
    dialogue: list[dict[str, str]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            continue

        if role == "user":
            if 'origin="idle_recap"' in content:
                continue
            if 'event="command"' in content:
                continue
            message_match = _MESSAGE_TAG_RE.search(content)
            if message_match:
                text = html.unescape(message_match.group(1)).strip()
                media_match = _MEDIA_DESCRIPTION_RE.search(content)
                if media_match:
                    description = html.unescape(media_match.group(1)).strip()
                    if description:
                        text = f"{text}\n[媒体描述] {description}".strip()
            elif "<metadata" in content:
                continue
            else:
                text = content.strip()
            if text:
                dialogue.append({"role": "user", "content": text})
            continue

        if role == "assistant":
            dialogue.append({"role": "assistant", "content": content.strip()})

    return dialogue[-IDLE_RECAP_MAX_DIALOGUE_MESSAGES:]


def _normalize_recap_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _parse_recap_response(value: object) -> dict[str, Any]:
    text = str(value or "").strip()
    if not text:
        raise ValueError("idle recap response was empty")
    try:
        parsed = IdleRecapOutput.model_validate_json(text)
    except ValidationError as exc:
        raise ValueError("idle recap response failed the required JSON schema") from exc

    result: dict[str, Any] = {
        "recap": _normalize_recap_text(parsed.recap),
        "open_loops": _normalize_recap_text(parsed.open_loops),
        "suggested_follow_up": _normalize_recap_text(parsed.suggested_follow_up),
        "memory_suggestion": {
            "impression": _normalize_recap_text(parsed.memory_suggestion.impression),
            "diary": _normalize_recap_text(parsed.memory_suggestion.diary),
        },
    }

    memory_suggestion = result["memory_suggestion"]
    if not any(
        [
            result["recap"],
            result["open_loops"],
            result["suggested_follow_up"],
            memory_suggestion["impression"],
            memory_suggestion["diary"],
        ]
    ):
        raise ValueError("idle recap response contains no usable content")
    return result


async def _generate_recap_with_retries(
    user_id: int,
    dialogue: list[dict[str, str]],
    memory_context: dict[str, Any],
) -> dict[str, Any]:
    transcript = json.dumps(dialogue, ensure_ascii=False)
    stored_memory = json.dumps(memory_context, ensure_ascii=False)
    prompt = (
        "根据下面近期对话生成一次短期回顾。"
        "现有长期记忆只用于判断候选内容是否已经记录。\n\n"
        f"近期对话：{transcript}\n\n"
        f"现有长期记忆：{stored_memory}"
    )
    messages = [{"role": "user", "content": prompt}]
    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "idle_recap",
            "strict": True,
            "schema": IdleRecapOutput.model_json_schema(),
        },
    }

    last_error: Exception | None = None
    for attempt in range(1, IDLE_RECAP_RETRY_LIMIT + 1):
        try:
            content = await _run_recap_agent(messages, user_id, response_format)
            return _parse_recap_response(content)
        except Exception as exc:
            last_error = exc
            logger.warning(
                "Idle recap generation attempt %s/%s failed: %s",
                attempt,
                IDLE_RECAP_RETRY_LIMIT,
                exc,
            )
    raise RuntimeError("Idle recap generation failed after retries") from last_error


async def _run_recap_agent(
    messages: list[dict[str, Any]],
    user_id: int,
    response_format: dict[str, Any],
) -> str:
    last_error: Exception | None = None
    for provider in get_provider_order_for_task("recap"):
        try:
            models = get_models_for_task(provider, "recap")
        except Exception as exc:
            logger.warning(
                "Idle recap skipped invalid provider=%s: %s",
                provider,
                exc,
            )
            last_error = exc
            continue

        for model in models:
            set_tool_request_context({"user_id": user_id})
            try:
                completion_kwargs = {
                    **completion_kwargs_for_task(provider, "recap"),
                    "response_format": response_format,
                    "drop_params": False,
                }
                content, _ = await run_tool_loop(
                    provider,
                    model,
                    messages,
                    {"user_id": user_id},
                    provider_name="Idle recap",
                    completion_timeout=IDLE_RECAP_TIMEOUT_SECONDS,
                    completion_kwargs=completion_kwargs,
                    tool_definitions=IDLE_RECAP_TOOLS,
                    tool_handlers=IDLE_RECAP_TOOL_HANDLERS,
                    system_prompt_override=config.IDLE_RECAP_SYSTEM_PROMPT,
                )
                return content
            except Exception as exc:
                logger.warning(
                    "Idle recap failed via provider=%s model=%s: %s",
                    provider,
                    model,
                    exc,
                )
                last_error = exc
            finally:
                clear_tool_request_context()

    raise RuntimeError("All providers failed for idle recap") from last_error


async def _generate_recap(
    user_id: int,
    dialogue: list[dict[str, str]],
    memory_context: dict[str, Any],
) -> dict[str, Any]:
    return await _generate_recap_with_retries(user_id, dialogue, memory_context)


async def _load_recap_memory_context(user_id: int) -> dict[str, Any]:
    impression = _normalize_recap_text(
        await process_user.async_get_user_impression(user_id)
    )
    rows = await mysql_connection.fetch_all(
        "SELECT page_no, title, summary FROM ai_user_diary_pages "
        "WHERE user_id = %s ORDER BY page_no ASC",
        (user_id,),
    )
    diary_index = [
        {
            "page": int(row[0]),
            "title": _normalize_recap_text(row[1]),
            "summary": _normalize_recap_text(row[2]),
        }
        for row in rows
    ]
    return {
        "impression": impression,
        "diary_index": diary_index,
    }


def _format_idle_recap_event(
    recap: dict[str, Any],
    *,
    timestamp: datetime,
) -> str:
    if timestamp.tzinfo is not None:
        timestamp = timestamp.astimezone(UTC).replace(tzinfo=None)
    attrs = [
        ("type", "idle_followup"),
        ("timestamp", timestamp.strftime("%Y-%m-%d %H:%M:%S")),
        ("origin", "idle_recap"),
    ]
    lines = [f"<metadata {format_metadata_attrs(attrs)}>"]
    if recap.get("recap"):
        lines.append(f"  <recap>{xml_escape(recap['recap'])}</recap>")
    if recap.get("open_loops"):
        lines.append(f"  <open_loops>{xml_escape(recap['open_loops'])}</open_loops>")
    if recap.get("suggested_follow_up"):
        lines.append(
            "  <suggested_follow_up>"
            f"{xml_escape(recap['suggested_follow_up'])}"
            "</suggested_follow_up>"
        )
    memory_suggestion = recap.get("memory_suggestion")
    if isinstance(memory_suggestion, dict):
        impression = _normalize_recap_text(memory_suggestion.get("impression"))
        diary = _normalize_recap_text(memory_suggestion.get("diary"))
        if impression or diary:
            lines.append("  <memory_suggestion>")
            if impression:
                lines.append(f"    <impression>{xml_escape(impression)}</impression>")
            if diary:
                lines.append(f"    <diary>{xml_escape(diary)}</diary>")
            lines.append("  </memory_suggestion>")
    lines.append("</metadata>")
    return "\n".join(lines)


async def note_incoming_private_message(user_id: int) -> None:
    """Invalidate an in-flight follow-up before the conversation lock is acquired."""

    if not IDLE_FOLLOWUP_ENABLED:
        return
    now = datetime.now(UTC).replace(tzinfo=None)
    try:
        await mysql_connection.execute(
            "UPDATE ai_idle_followups "
            "SET last_activity_at = %s, "
            "next_run_at = DATE_ADD(%s, INTERVAL typical_interval_seconds SECOND), "
            "activity_version = activity_version + 1, status = 'fired', "
            "claim_until = NULL, claim_token = NULL, claim_attempts = 0, stage = 'idle', "
            "retry_count = 0, last_error = NULL "
            "WHERE user_id = %s",
            (now, now, user_id),
        )
    except Exception:
        logger.exception("Failed to refresh idle follow-up activity: user_id=%s", user_id)


async def arm_from_private_turn(user_id: int) -> None:
    """Record one accepted private AI turn and arm its one-shot idle follow-up."""

    if not IDLE_FOLLOWUP_ENABLED:
        return
    now = datetime.now(UTC).replace(tzinfo=None)
    try:
        async with mysql_connection.transaction() as connection:
            row = await mysql_connection.fetch_one(
                "SELECT last_turn_at, recent_intervals "
                "FROM ai_idle_followups WHERE user_id = %s FOR UPDATE",
                (user_id,),
                connection=connection,
            )
            intervals: list[int] = []
            if row:
                last_turn_at = row[0]
                intervals = _decode_recent_intervals(row[1])
                if last_turn_at:
                    gap_seconds = int((now - last_turn_at).total_seconds())
                    if gap_seconds > 0:
                        intervals.append(gap_seconds)
                        intervals = intervals[-IDLE_FOLLOWUP_SAMPLE_SIZE:]

            ttl_seconds = calculate_ttl_seconds(intervals)
            next_run_at = now + timedelta(seconds=ttl_seconds)
            intervals_json = json.dumps(intervals, ensure_ascii=False)
            if row:
                await connection.exec_driver_sql(
                    "UPDATE ai_idle_followups "
                    "SET last_activity_at = %s, last_turn_at = %s, next_run_at = %s, "
                    "typical_interval_seconds = %s, recent_intervals = %s, "
                    "activity_version = activity_version + 1, status = 'armed', "
                    "claim_until = NULL, claim_token = NULL, claim_attempts = 0, "
                    "stage = 'idle', retry_count = 0, last_error = NULL "
                    "WHERE user_id = %s",
                    (
                        now,
                        now,
                        next_run_at,
                        ttl_seconds,
                        intervals_json,
                        user_id,
                    ),
                )
            else:
                await connection.exec_driver_sql(
                    "INSERT INTO ai_idle_followups "
                    "(user_id, last_activity_at, last_turn_at, next_run_at, "
                    "typical_interval_seconds, recent_intervals, activity_version, status) "
                    "VALUES (%s, %s, %s, %s, %s, %s, 1, 'armed')",
                    (
                        user_id,
                        now,
                        now,
                        next_run_at,
                        ttl_seconds,
                        intervals_json,
                    ),
                )
    except Exception:
        logger.exception("Failed to arm idle follow-up: user_id=%s", user_id)


async def cancel_idle_followup(user_id: int) -> None:
    if not IDLE_FOLLOWUP_ENABLED:
        return
    try:
        await mysql_connection.execute(
            "DELETE FROM ai_idle_followups WHERE user_id = %s",
            (user_id,),
        )
    except Exception:
        logger.exception("Failed to cancel idle follow-up: user_id=%s", user_id)


async def _claim_due_followups(
    limit: int = IDLE_FOLLOWUP_BATCH_SIZE,
) -> list[IdleFollowupClaim]:
    """claim 到期的跟进：每个都有新 token、新租约，并在同一事务里写下尝试记录。

    claim 以 activity_version 为条件：用户在这期间有了新活动就不会 claim 到。
    租约已到期的 executing 行先由 `_recover_expired_followups` 按阶段处理。
    """
    now = datetime.now(UTC).replace(tzinfo=None)
    claims: list[IdleFollowupClaim] = []
    async with mysql_connection.transaction() as connection:
        rows = await mysql_connection.fetch_all(
            "SELECT f.user_id, f.activity_version, f.retry_count, f.claim_attempts "
            "FROM ai_idle_followups AS f "
            "LEFT JOIN user AS u ON u.id = f.user_id "
            "WHERE f.status = 'armed' AND f.next_run_at <= %s "
            "AND (u.id IS NULL OR "
            "COALESCE(u.coins, 0) + COALESCE(u.coins_paid, 0) > 0) "
            "ORDER BY f.next_run_at ASC, f.user_id ASC LIMIT %s "
            "FOR UPDATE OF f SKIP LOCKED",
            (now, limit),
            connection=connection,
        )
        for row in rows:
            token = job_claims.new_claim_token()
            attempt = int(row[3] or 0) + 1
            result = await connection.exec_driver_sql(
                "UPDATE ai_idle_followups "
                "SET status = 'executing', stage = 'claimed', claim_token = %s, "
                "claim_until = DATE_ADD(UTC_TIMESTAMP(), INTERVAL %s SECOND), "
                "claim_attempts = %s "
                "WHERE user_id = %s AND activity_version = %s AND status = 'armed'",
                (token, int(IDLE_FOLLOWUP_LEASE_SECONDS), attempt, row[0], row[1]),
            )
            if result.rowcount != 1:
                continue
            await job_claims.open_attempt(
                connection,
                job_claims.IDLE_FOLLOWUP_JOB,
                int(row[0]),
                token,
                attempt,
                job_version=int(row[1]),
            )
            claims.append(
                IdleFollowupClaim(
                    user_id=int(row[0]),
                    activity_version=int(row[1]),
                    retry_count=int(row[2] or 0),
                    token=token,
                    attempt=attempt,
                )
            )
    return claims


_OWNED_CLAIM_WHERE = (
    "WHERE user_id = %s AND claim_token = %s AND activity_version = %s "
    "AND status = 'executing'"
)


def _claim_params(claim: IdleFollowupClaim) -> tuple:
    return (claim.user_id, claim.token, claim.activity_version)


async def _claim_is_current(claim: IdleFollowupClaim) -> bool:
    row = await mysql_connection.fetch_one(
        "SELECT 1 FROM ai_idle_followups " + _OWNED_CLAIM_WHERE,
        _claim_params(claim),
    )
    return bool(row)


async def _enter_stage(run: _IdleRun, stage: str) -> None:
    """推进阶段并更新尝试记录，同一个事务；claim 已失效则抛 ClaimLostError。"""
    claim = run.claim
    async with mysql_connection.transaction() as connection:
        await job_claims.advance_stage(
            connection,
            job_claims.IDLE_FOLLOWUP_JOB,
            claim.user_id,
            claim.token,
            stage,
            extra_where=" AND activity_version = %s",
            extra_params=(claim.activity_version,),
        )
    run.stage = stage


async def _mark_claim_fired(
    claim: IdleFollowupClaim,
    *,
    outcome: str = job_claims.OUTCOME_COMPLETED,
    error: str | None = None,
) -> None:
    """终结 claim 并关闭尝试记录；claim 已失效（被回收或用户有了新活动）抛 ClaimLostError。"""
    async with mysql_connection.transaction() as connection:
        result = await connection.exec_driver_sql(
            "UPDATE ai_idle_followups "
            "SET status = 'fired', claim_until = NULL, claim_token = NULL, stage = 'idle', "
            "claim_attempts = 0, last_fired_at = UTC_TIMESTAMP(), last_error = %s "
            + _OWNED_CLAIM_WHERE,
            (None if error is None else job_claims.truncate_error(error), *_claim_params(claim)),
        )
        if result.rowcount != 1:
            raise ClaimLostError(f"idle follow-up {claim.user_id} is no longer owned by this claim")
        await job_claims.close_attempt(
            connection,
            claim.token,
            outcome,
            stage=job_claims.STAGE_COMPLETED if outcome == job_claims.OUTCOME_COMPLETED else None,
            error=error,
        )


async def _release_claim(
    claim: IdleFollowupClaim,
    outcome: str,
    *,
    attempts_expr: str,
) -> None:
    """把还没产生副作用的 claim 放回 armed（仍然到期，下一次轮询重新 claim）。"""
    async with mysql_connection.transaction() as connection:
        result = await connection.exec_driver_sql(
            "UPDATE ai_idle_followups "
            "SET status = 'armed', stage = 'idle', claim_token = NULL, claim_until = NULL, "
            f"claim_attempts = {attempts_expr}, last_error = NULL "
            + _OWNED_CLAIM_WHERE,
            _claim_params(claim),
        )
        if result.rowcount != 1:
            raise ClaimLostError(f"idle follow-up {claim.user_id} is no longer owned by this claim")
        await job_claims.close_attempt(connection, claim.token, outcome)


async def _pause_claim_until_coins_available(claim: IdleFollowupClaim) -> None:
    await _release_claim(
        claim,
        job_claims.OUTCOME_PAUSED,
        attempts_expr=_ATTEMPTS_RESET,
    )


async def _get_followup_user_total_coins(user_id: int) -> int | None:
    row = await mysql_connection.fetch_one(
        "SELECT coins, coins_paid FROM user WHERE id = %s",
        (user_id,),
    )
    if not row:
        return None
    return (row[0] or 0) + (row[1] or 0)


async def _record_claim_failure(run: _IdleRun, exc: BaseException) -> None:
    """已知失败的收尾：没有副作用才重试，否则不再重跑。

    重试要求失败发生在 claimed 阶段（只读的回顾生成），或者主模型明确失败且没有执行过任何工具。
    其余失败发生在工具或投递可能已经产生副作用之后，重跑会重放它们，所以直接结束。
    """
    claim = run.claim
    error_text = job_claims.error_summary(exc)
    next_retry_count = claim.retry_count + 1
    retry_safe = run.stage == job_claims.STAGE_CLAIMED or isinstance(
        exc, _NoSideEffectsError
    )
    async with mysql_connection.transaction() as connection:
        if retry_safe and next_retry_count < IDLE_FOLLOWUP_MAX_RETRIES:
            next_run_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(
                minutes=IDLE_FOLLOWUP_RETRY_MINUTES
            )
            result = await connection.exec_driver_sql(
                "UPDATE ai_idle_followups "
                "SET status = 'armed', stage = 'idle', next_run_at = %s, "
                "claim_token = NULL, claim_until = NULL, retry_count = %s, last_error = %s "
                + _OWNED_CLAIM_WHERE,
                (next_run_at, next_retry_count, error_text, *_claim_params(claim)),
            )
            outcome = job_claims.OUTCOME_RETRY
        else:
            result = await connection.exec_driver_sql(
                "UPDATE ai_idle_followups "
                "SET status = 'fired', stage = 'idle', claim_token = NULL, claim_until = NULL, "
                "claim_attempts = 0, retry_count = %s, last_fired_at = UTC_TIMESTAMP(), "
                "last_error = %s " + _OWNED_CLAIM_WHERE,
                (next_retry_count, error_text, *_claim_params(claim)),
            )
            outcome = job_claims.OUTCOME_FAILED
        if result.rowcount != 1:
            raise ClaimLostError(f"idle follow-up {claim.user_id} is no longer owned by this claim")
        await job_claims.close_attempt(connection, claim.token, outcome, error=error_text)


async def _persist_completed_turn(
    claim: IdleFollowupClaim,
    recap_event: str,
    assistant_message: str,
    tool_logs: list[dict],
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    records = [("user", recap_event)]
    records.extend(tool_logs_to_record_entries(tool_logs))
    if assistant_message:
        records.append(("assistant", assistant_message))
    snapshot_created, _, archived_records = await mysql_connection.async_insert_chat_records(
        claim.user_id,
        records,
    )
    if snapshot_created:
        summary.schedule_summary_generation(claim.user_id)
    if archived_records:
        await send_permanent_records_archive(
            context.bot,
            claim.user_id,
            archived_records,
            logger=logger,
        )


async def _send_followup_outputs(
    user_id: int,
    assistant_message: str,
    tool_logs: list[dict],
    context: ContextTypes.DEFAULT_TYPE,
) -> str | None:
    """投递跟进内容；已知的投递失败只记录并返回摘要，不影响本地结果的落库。"""
    delivery_errors: list[str] = []
    if assistant_message:
        try:
            await context.bot.send_chat_action(chat_id=user_id, action="typing")
        except Exception:
            logger.debug("Failed to send typing action for idle follow-up: user_id=%s", user_id)

        send_func = partial_send(context.bot.send_message, user_id)
        try:
            with suppress_telegram_history():
                await send_ai_reply_with_stickers(
                    bot=context.bot,
                    chat_id=user_id,
                    text=assistant_message,
                    first_text_send=send_func,
                    fallback_send=send_func,
                    logger=logger,
                )
        except PartialAIReplySendError as exc:
            logger.warning(
                "Idle follow-up was only partially sent: user_id=%s sent_messages=%s error=%s",
                user_id,
                len(exc.sent_messages),
                exc,
            )
            delivery_errors.append(job_claims.error_summary(exc))
        except Exception as exc:
            logger.exception("Failed to send idle follow-up reply: user_id=%s", user_id)
            delivery_errors.append(job_claims.error_summary(exc))

    try:
        await send_generated_media(
            bot=context.bot,
            chat_id=user_id,
            tool_logs=tool_logs,
            logger=logger,
        )
    except Exception as exc:
        logger.exception("Failed to send idle follow-up tool media: user_id=%s", user_id)
        delivery_errors.append(job_claims.error_summary(exc))
    return "; ".join(delivery_errors) or None


async def _recover_expired_followups() -> int:
    """回收租约已到期的 executing 跟进（崩溃、卡死或被撤销的 worker 留下的）。

    - claimed 阶段：只做过只读的回顾生成，放回 armed 重新 claim；反复到期超过上限则放弃。
    - generating / delivering 阶段：工具或投递可能已经产生副作用，结果未知，
      不重跑整轮、不重发，直接记为 fired。
    每次回收都会给下一次 claim 生成新的 token，旧 worker 的写入因此全部被拒绝。
    """
    recovered = 0
    async with mysql_connection.transaction() as connection:
        rows = await mysql_connection.fetch_all(
            "SELECT user_id, stage, claim_token, claim_attempts "
            "FROM ai_idle_followups "
            "WHERE status = 'executing' AND claim_until <= UTC_TIMESTAMP() "
            "ORDER BY claim_until ASC, user_id ASC LIMIT %s FOR UPDATE SKIP LOCKED",
            (RECOVERY_BATCH_SIZE,),
            connection=connection,
        )
        for user_id, stage, token, attempts in rows:
            attempts = int(attempts or 0)
            if stage == job_claims.STAGE_CLAIMED and attempts < IDLE_FOLLOWUP_MAX_CLAIM_ATTEMPTS:
                result = await connection.exec_driver_sql(
                    "UPDATE ai_idle_followups SET status = 'armed', stage = 'idle', "
                    "claim_token = NULL, claim_until = NULL "
                    "WHERE user_id = %s AND claim_token <=> %s AND status = 'executing'",
                    (user_id, token),
                )
                if result.rowcount == 1 and token:
                    await job_claims.close_attempt(
                        connection, token, job_claims.OUTCOME_EXPIRED
                    )
                logger.warning(
                    "Recovered expired idle follow-up claim for a safe re-run: "
                    "user_id=%s attempt=%s",
                    user_id,
                    attempts,
                )
            else:
                if stage == job_claims.STAGE_CLAIMED:
                    outcome = job_claims.OUTCOME_ABANDONED
                    error = ABANDONED_ERROR.format(attempts=attempts)
                else:
                    outcome = job_claims.OUTCOME_UNKNOWN
                    error = INTERRUPTED_ERROR.format(stage=stage)
                result = await connection.exec_driver_sql(
                    "UPDATE ai_idle_followups SET status = 'fired', stage = 'idle', "
                    "claim_token = NULL, claim_until = NULL, claim_attempts = 0, "
                    "last_fired_at = UTC_TIMESTAMP(), last_error = %s "
                    "WHERE user_id = %s AND claim_token <=> %s AND status = 'executing'",
                    (job_claims.truncate_error(error), user_id, token),
                )
                if result.rowcount == 1 and token:
                    await job_claims.close_attempt(connection, token, outcome, error=error)
                logger.warning(
                    "Idle follow-up interrupted; not retried: user_id=%s stage=%s outcome=%s",
                    user_id,
                    stage,
                    outcome,
                )
            recovered += 1
    return recovered


async def _run_housekeeping() -> None:
    try:
        await _recover_expired_followups()
        await job_claims.sweep_orphaned_attempts(job_claims.IDLE_FOLLOWUP_JOB)
    except Exception:
        logger.exception("Idle follow-up recovery pass failed")


def _application_stopping(context: Any) -> bool:
    """应用已经开始停止：不再 claim 新跟进，尚未开始的 claim 直接释放。"""
    application = getattr(context, "application", None)
    return application is not None and not application.running


async def _release_if_unstarted(run: _IdleRun) -> None:
    if run.stage != job_claims.STAGE_CLAIMED:
        return
    try:
        await asyncio.shield(
            _release_claim(
                run.claim,
                job_claims.OUTCOME_RELEASED,
                attempts_expr=_ATTEMPTS_REFUND,
            )
        )
    except Exception:
        # 释放不了就等租约到期，claimed 阶段会被安全重跑。
        logger.warning(
            "Could not release idle follow-up claim on shutdown: user_id=%s",
            run.claim.user_id,
        )


async def _process_claim(
    claim: IdleFollowupClaim,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """在租约保护下处理一个已经 claim 的跟进。"""
    run = _IdleRun(claim)
    try:
        await job_claims.run_leased(
            _run_claim(run, context),
            renew=lambda: job_claims.renew_lease(
                job_claims.IDLE_FOLLOWUP_JOB,
                claim.user_id,
                claim.token,
                IDLE_FOLLOWUP_LEASE_SECONDS,
            ),
            lease_seconds=IDLE_FOLLOWUP_LEASE_SECONDS,
            heartbeat_seconds=IDLE_FOLLOWUP_HEARTBEAT_SECONDS,
            timeout=IDLE_FOLLOWUP_EXECUTION_TIMEOUT_SECONDS,
            abort_event=run.abort_event,
            label=f"idle follow-up {claim.user_id}",
        )
    except ClaimLostError:
        logger.warning(
            "Idle follow-up no longer owns its claim; stopped without further writes: "
            "user_id=%s activity_version=%s",
            claim.user_id,
            claim.activity_version,
        )
    except asyncio.CancelledError:
        await _release_if_unstarted(run)
        raise
    except Exception as exc:
        logger.exception(
            "Idle follow-up failed: user_id=%s activity_version=%s",
            claim.user_id,
            claim.activity_version,
        )
        try:
            await _record_claim_failure(run, exc)
        except ClaimLostError:
            logger.warning(
                "Idle follow-up lost its claim before the failure could be recorded: user_id=%s",
                claim.user_id,
            )
        except Exception:
            # 写不进去也没关系：租约到期后按所处阶段回收。
            logger.exception(
                "Failed to record idle follow-up failure: user_id=%s activity_version=%s",
                claim.user_id,
                claim.activity_version,
            )


async def _run_claim(
    run: _IdleRun,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    claim = run.claim
    async with get_conversation_lock(claim.user_id):
        if not await _claim_is_current(claim):
            return

        if _application_stopping(context):
            await _release_claim(
                claim,
                job_claims.OUTCOME_RELEASED,
                attempts_expr=_ATTEMPTS_REFUND,
            )
            return

        total_coins = await _get_followup_user_total_coins(claim.user_id)
        if total_coins is None:
            await _mark_claim_fired(
                claim,
                outcome=job_claims.OUTCOME_FAILED,
                error="user not found",
            )
            return
        if total_coins < 1:
            await _pause_claim_until_coins_available(claim)
            logger.info(
                "Idle follow-up paused until coins are available: user_id=%s",
                claim.user_id,
            )
            return

        chat_history = await mysql_connection.async_get_chat_history(claim.user_id)
        dialogue = _extract_recent_dialogue(chat_history)
        if not dialogue:
            await _mark_claim_fired(claim)
            return

        # 回顾生成只用只读工具，崩溃后重跑没有副作用，所以仍处于 claimed 阶段。
        memory_context = await _load_recap_memory_context(claim.user_id)
        recap = await _generate_recap(claim.user_id, dialogue, memory_context)
        if not await _claim_is_current(claim):
            return

        recap_event = _format_idle_recap_event(
            recap,
            timestamp=datetime.now(UTC),
        )
        user_state_prompt = await build_user_state_prompt(claim.user_id)
        if user_state_prompt is None:
            await _mark_claim_fired(
                claim,
                outcome=job_claims.OUTCOME_FAILED,
                error="user not found",
            )
            return

        ai_messages = list(chat_history)
        ai_messages.append({"role": "user", "content": recap_event})
        # 主模型带完整工具集：从这里开始可能产生副作用，崩溃后不再重跑整轮。
        await _enter_stage(run, job_claims.STAGE_GENERATING)
        assistant_message, tool_logs = await ai_chat.get_ai_response(
            ai_messages,
            claim.user_id,
            tool_context={
                "is_group": False,
                "group_id": None,
                "message_id": None,
                "user_id": claim.user_id,
                "user_state_prompt": user_state_prompt,
                ABORT_EVENT_KEY: run.abort_event,
            },
        )
        assistant_message = normalize_ai_reply_text(assistant_message)
        failure_cause = runtime_error_cause(assistant_message)
        if failure_cause:
            if not tool_logs:
                raise _NoSideEffectsError(
                    f"main AI failed during idle follow-up: {failure_cause}"
                )
            logger.warning(
                "Idle follow-up main AI failed after tool execution: user_id=%s cause=%s",
                claim.user_id,
                failure_cause,
            )
            assistant_message = ""
        if assistant_message:
            assistant_message = await normalize_sticker_directives(
                assistant_message,
                logger=logger,
            )

        if not await _claim_is_current(claim):
            if tool_logs:
                # 用户已经回来了，跟进不再发送；已经执行过的工具仍要写进历史。
                await _persist_completed_turn(
                    claim,
                    recap_event,
                    "",
                    tool_logs,
                    context,
                )
            return

        # 回复文本与工具结果此时只在内存里；进入 delivering 后崩溃按结果未知处理，不重发。
        await _enter_stage(run, job_claims.STAGE_DELIVERING)
        await _persist_completed_turn(
            claim,
            recap_event,
            assistant_message,
            tool_logs,
            context,
        )
        delivery_error = await _send_followup_outputs(
            claim.user_id,
            assistant_message,
            tool_logs,
            context,
        )
        await _mark_claim_fired(claim, error=delivery_error)


async def run_idle_followup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not IDLE_FOLLOWUP_ENABLED or _idle_followup_job_lock.locked():
        return

    async with _idle_followup_job_lock:
        await _run_housekeeping()
        if _application_stopping(context):
            return
        claims = await _claim_due_followups()
        if claims:
            await asyncio.gather(*(_process_claim(claim, context) for claim in claims))


__all__ = [
    "IDLE_FOLLOWUP_POLL_INTERVAL",
    "arm_from_private_turn",
    "calculate_ttl_seconds",
    "cancel_idle_followup",
    "note_incoming_private_message",
    "run_idle_followup_job",
]


def setup_idle_followup_jobs(application) -> None:
    """注册空闲跟进轮询。"""

    application.job_queue.run_repeating(
        run_idle_followup_job,
        interval=IDLE_FOLLOWUP_POLL_INTERVAL,
        first=15,
    )
