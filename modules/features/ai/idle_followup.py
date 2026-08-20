"""Idle private-chat recap and one-shot follow-up handling."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from telegram.ext import ContextTypes

from core import config, mysql_connection, process_user
from core.archive_utils import send_permanent_records_archive
from core.prompt_utils import format_metadata_attrs, xml_escape
from core.telegram_history import suppress_telegram_history
from core.telegram_utils import partial_send
from features.ai import ai_chat, summary
from features.ai.conversation_locks import get_conversation_lock
from features.ai.outbound import send_generated_media
from features.ai.provider_resolver import (
    completion_kwargs_for_task,
    get_models_for_task,
    get_provider_order_for_task,
)
from features.ai.reply_filter import normalize_ai_reply_text
from features.ai.router import runtime_error_cause
from features.ai.runtime import EXECUTOR
from features.ai.sticker_sender import (
    PartialAIReplySendError,
    normalize_sticker_directives,
    send_ai_reply_with_stickers,
)
from features.ai.tool_history import tool_logs_to_record_entries
from features.ai.tool_runner import run_tool_loop
from features.ai.tools import (
    AI_TOOL_HANDLERS,
    OPENAI_TOOLS,
    clear_tool_request_context,
    set_tool_request_context,
)
from features.ai.tools.memory_tools import read_diary_page_tool
from features.ai.tools.schemas import IDLE_RECAP_READ_DIARY_TOOL
from features.ai.user_state import build_user_state_prompt

logger = logging.getLogger(__name__)

IDLE_FOLLOWUP_POLL_INTERVAL = 60
IDLE_FOLLOWUP_BATCH_SIZE = 3
IDLE_FOLLOWUP_SAMPLE_SIZE = 5
IDLE_FOLLOWUP_ENABLED = True
IDLE_FOLLOWUP_DEFAULT_MINUTES = 10
IDLE_FOLLOWUP_MIN_MINUTES = 2
IDLE_FOLLOWUP_MAX_MINUTES = 60
IDLE_FOLLOWUP_CLAIM_MINUTES = 15
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
    user_id: int
    activity_version: int
    retry_count: int


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


def _generate_recap_sync(
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
            content = _run_recap_agent(messages, user_id, response_format)
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


def _run_recap_agent(
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
                content, _ = run_tool_loop(
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
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        EXECUTOR,
        _generate_recap_sync,
        user_id,
        dialogue,
        memory_context,
    )


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
        timestamp = timestamp.astimezone(timezone.utc).replace(tzinfo=None)
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
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        await mysql_connection.execute(
            "UPDATE ai_idle_followups "
            "SET last_activity_at = %s, "
            "next_run_at = DATE_ADD(%s, INTERVAL typical_interval_seconds SECOND), "
            "activity_version = activity_version + 1, status = 'fired', "
            "claim_until = NULL, retry_count = 0, last_error = NULL "
            "WHERE user_id = %s",
            (now, now, user_id),
        )
    except Exception:
        logger.exception("Failed to refresh idle follow-up activity: user_id=%s", user_id)


async def arm_from_private_turn(user_id: int) -> None:
    """Record one accepted private AI turn and arm its one-shot idle follow-up."""

    if not IDLE_FOLLOWUP_ENABLED:
        return
    now = datetime.now(timezone.utc).replace(tzinfo=None)
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
                    "claim_until = NULL, retry_count = 0, last_error = NULL "
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
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    claim_until = now + timedelta(minutes=IDLE_FOLLOWUP_CLAIM_MINUTES)
    async with mysql_connection.transaction() as connection:
        rows = await mysql_connection.fetch_all(
            "SELECT f.user_id, f.activity_version, f.retry_count "
            "FROM ai_idle_followups AS f "
            "LEFT JOIN user AS u ON u.id = f.user_id "
            "WHERE ((f.status = 'armed' AND f.next_run_at <= %s) "
            "OR (f.status = 'executing' AND f.claim_until IS NOT NULL "
            "AND f.claim_until <= %s)) "
            "AND (u.id IS NULL OR "
            "COALESCE(u.coins, 0) + COALESCE(u.coins_paid, 0) > 0) "
            "ORDER BY f.next_run_at ASC, f.user_id ASC LIMIT %s FOR UPDATE",
            (now, now, limit),
            connection=connection,
        )
        claims = [
            IdleFollowupClaim(
                user_id=int(row[0]),
                activity_version=int(row[1]),
                retry_count=int(row[2] or 0),
            )
            for row in rows
        ]
        for claim in claims:
            await connection.exec_driver_sql(
                "UPDATE ai_idle_followups SET status = 'executing', claim_until = %s "
                "WHERE user_id = %s AND activity_version = %s",
                (claim_until, claim.user_id, claim.activity_version),
            )
    return claims


async def _claim_is_current(claim: IdleFollowupClaim) -> bool:
    row = await mysql_connection.fetch_one(
        "SELECT 1 FROM ai_idle_followups "
        "WHERE user_id = %s AND activity_version = %s AND status = 'executing'",
        (claim.user_id, claim.activity_version),
    )
    return bool(row)


async def _mark_claim_fired(claim: IdleFollowupClaim) -> None:
    await mysql_connection.execute(
        "UPDATE ai_idle_followups "
        "SET status = 'fired', claim_until = NULL, last_fired_at = UTC_TIMESTAMP(), "
        "last_error = NULL "
        "WHERE user_id = %s AND activity_version = %s AND status = 'executing'",
        (claim.user_id, claim.activity_version),
    )


async def _pause_claim_until_coins_available(claim: IdleFollowupClaim) -> None:
    await mysql_connection.execute(
        "UPDATE ai_idle_followups "
        "SET status = 'armed', claim_until = NULL, last_error = NULL "
        "WHERE user_id = %s AND activity_version = %s AND status = 'executing'",
        (claim.user_id, claim.activity_version),
    )


async def _get_followup_user_total_coins(user_id: int) -> int | None:
    row = await mysql_connection.fetch_one(
        "SELECT coins, coins_paid FROM user WHERE id = %s",
        (user_id,),
    )
    if not row:
        return None
    return (row[0] or 0) + (row[1] or 0)


async def _record_claim_failure(claim: IdleFollowupClaim, exc: Exception) -> None:
    error_text = re.sub(r"\s+", " ", str(exc)).strip() or type(exc).__name__
    error_text = error_text[:500]
    next_retry_count = claim.retry_count + 1
    if next_retry_count >= IDLE_FOLLOWUP_MAX_RETRIES:
        await mysql_connection.execute(
            "UPDATE ai_idle_followups "
            "SET status = 'fired', claim_until = NULL, retry_count = %s, "
            "last_fired_at = UTC_TIMESTAMP(), last_error = %s "
            "WHERE user_id = %s AND activity_version = %s AND status = 'executing'",
            (
                next_retry_count,
                error_text,
                claim.user_id,
                claim.activity_version,
            ),
        )
        return

    next_run_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(
        minutes=IDLE_FOLLOWUP_RETRY_MINUTES
    )
    await mysql_connection.execute(
        "UPDATE ai_idle_followups "
        "SET status = 'armed', next_run_at = %s, claim_until = NULL, "
        "retry_count = %s, last_error = %s "
        "WHERE user_id = %s AND activity_version = %s AND status = 'executing'",
        (
            next_run_at,
            next_retry_count,
            error_text,
            claim.user_id,
            claim.activity_version,
        ),
    )


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
    await _mark_claim_fired(claim)
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
) -> None:
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
        except Exception:
            logger.exception("Failed to send idle follow-up reply: user_id=%s", user_id)

    try:
        await send_generated_media(
            bot=context.bot,
            chat_id=user_id,
            tool_logs=tool_logs,
            logger=logger,
        )
    except Exception:
        logger.exception("Failed to send idle follow-up tool media: user_id=%s", user_id)


async def _process_claim(
    claim: IdleFollowupClaim,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    try:
        async with get_conversation_lock(claim.user_id):
            if not await _claim_is_current(claim):
                return

            total_coins = await _get_followup_user_total_coins(claim.user_id)
            if total_coins is None:
                await _mark_claim_fired(claim)
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

            memory_context = await _load_recap_memory_context(claim.user_id)
            recap = await _generate_recap(claim.user_id, dialogue, memory_context)
            if not await _claim_is_current(claim):
                return

            recap_event = _format_idle_recap_event(
                recap,
                timestamp=datetime.now(timezone.utc),
            )
            user_state_prompt = await build_user_state_prompt(claim.user_id)
            if user_state_prompt is None:
                await _mark_claim_fired(claim)
                return

            ai_messages = list(chat_history)
            ai_messages.append({"role": "user", "content": recap_event})
            assistant_message, tool_logs = await ai_chat.get_ai_response(
                ai_messages,
                claim.user_id,
                tool_context={
                    "is_group": False,
                    "group_id": None,
                    "message_id": None,
                    "user_id": claim.user_id,
                    "user_state_prompt": user_state_prompt,
                },
            )
            assistant_message = normalize_ai_reply_text(assistant_message)
            failure_cause = runtime_error_cause(assistant_message)
            if failure_cause:
                if not tool_logs:
                    raise RuntimeError(
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

            if not await _claim_is_current(claim) and not tool_logs:
                return

            await _persist_completed_turn(
                claim,
                recap_event,
                assistant_message,
                tool_logs,
                context,
            )
            await _send_followup_outputs(
                claim.user_id,
                assistant_message,
                tool_logs,
                context,
            )
    except Exception as exc:
        logger.exception(
            "Idle follow-up failed: user_id=%s activity_version=%s",
            claim.user_id,
            claim.activity_version,
        )
        try:
            await _record_claim_failure(claim, exc)
        except Exception:
            logger.exception(
                "Failed to record idle follow-up failure: user_id=%s activity_version=%s",
                claim.user_id,
                claim.activity_version,
            )


async def run_idle_followup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not IDLE_FOLLOWUP_ENABLED or _idle_followup_job_lock.locked():
        return

    async with _idle_followup_job_lock:
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
