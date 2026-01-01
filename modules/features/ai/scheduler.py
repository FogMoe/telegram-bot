import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from telegram.ext import ContextTypes

from core import mysql_connection, process_user
from core.archive_utils import send_permanent_records_archive
from core.prompt_utils import format_user_state_prompt, xml_escape
from core.telegram_utils import partial_send, safe_send_markdown, split_ai_reply
from features.ai import ai_chat, summary

logger = logging.getLogger(__name__)

SCHEDULE_POLL_INTERVAL = 60
SCHEDULE_BATCH_SIZE = 5

_schedule_lock = asyncio.Lock()


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
        ("type", "private"),
        ("timestamp", _format_timestamp(timestamp)),
        ("user", "@scheduler"),
        ("origin", "scheduled_task"),
    ]
    if scheduled_at:
        attrs.append(("scheduled_at", _format_timestamp(scheduled_at)))
    if scheduled_for:
        attrs.append(("scheduled_for", _format_timestamp(scheduled_for)))

    attr_text = " ".join(
        f'{key}="{xml_escape(value)}"' for key, value in attrs if value
    )
    lines = [f"<metadata {attr_text}>"]
    lines.append(f"  <trigger>{xml_escape(trigger_reason)}</trigger>")
    if context_text:
        lines.append(f"  <context>{xml_escape(context_text)}</context>")
    lines.append(f"  <instruction>{xml_escape(instruction)}</instruction>")
    lines.append("</metadata>")
    return "\n".join(lines)


async def _build_user_state_prompt(user_id: int) -> Optional[str]:
    row = await mysql_connection.fetch_one(
        "SELECT permission, coins, info FROM user WHERE id = %s",
        (user_id,),
    )
    if not row:
        return None

    user_permission = row[0]
    user_coins = row[1]
    user_info_raw = row[2] if len(row) > 2 else ""

    user_affection = await process_user.async_get_user_affection(user_id)
    user_impression_raw = await process_user.async_get_user_impression(user_id)

    impression_display = (user_impression_raw or "").strip()
    if impression_display:
        impression_display = impression_display.replace("\r", " ").replace("\n", " ")
        if len(impression_display) > 500:
            impression_display = impression_display[:497] + "..."
    else:
        impression_display = "未记录"

    personal_info_display = (user_info_raw or "").strip()
    if personal_info_display and len(personal_info_display) > 500:
        personal_info_display = personal_info_display[:500]

    diary_row = await mysql_connection.fetch_one(
        "SELECT 1 FROM ai_user_diary_pages WHERE user_id = %s AND content != '' LIMIT 1",
        (user_id,),
    )
    diary_exists = bool(diary_row)

    return format_user_state_prompt(
        user_coins=user_coins,
        user_permission=user_permission,
        user_affection=user_affection,
        impression=impression_display,
        personal_info=personal_info_display,
        diary_exists=diary_exists,
    )


async def _mark_schedule_status(
    schedule_id: int,
    status: str,
    *,
    error: Optional[str] = None,
) -> None:
    if error is not None:
        await mysql_connection.execute(
            "UPDATE ai_schedules SET status = %s, error = %s WHERE id = %s",
            (status, error, schedule_id),
        )
        return

    if status == "executed":
        await mysql_connection.execute(
            "UPDATE ai_schedules SET status = %s, executed_at = UTC_TIMESTAMP() WHERE id = %s",
            (status, schedule_id),
        )
        return

    await mysql_connection.execute(
        "UPDATE ai_schedules SET status = %s WHERE id = %s",
        (status, schedule_id),
    )


async def _persist_tool_logs(
    conversation_id: int,
    tool_logs: list[dict],
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
) -> None:
    pending_tool_call_ids: list[str] = []

    for tool_log in tool_logs:
        entry_type = tool_log.get("type", "tool_result")
        tool_call_id = tool_log.get("tool_call_id")
        if not tool_call_id:
            if entry_type == "tool_result" and pending_tool_call_ids:
                tool_call_id = pending_tool_call_ids.pop(0)
            else:
                tool_call_id = f"auto_{int(time.time() * 1000)}"
        if entry_type == "assistant_tool_call":
            pending_tool_call_ids.append(tool_call_id)
            tool_log["tool_call_id"] = tool_call_id

        if entry_type == "assistant_tool_call":
            arguments = tool_log.get("arguments") or {}
            try:
                arguments_json = json.dumps(arguments, ensure_ascii=False)
            except TypeError:
                arguments_json = json.dumps({}, ensure_ascii=False)

            assistant_call_message = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": tool_call_id,
                        "type": "function",
                        "function": {
                            "name": tool_log.get("tool_name"),
                            "arguments": arguments_json,
                        },
                    }
                ],
            }

            snapshot_created, _, archived_records = await mysql_connection.async_insert_chat_record(
                conversation_id,
                "assistant",
                assistant_call_message,
            )
        else:
            if pending_tool_call_ids and pending_tool_call_ids[0] == tool_call_id:
                pending_tool_call_ids.pop(0)
            tool_result = tool_log.get("result")
            try:
                tool_result_str = json.dumps(tool_result, ensure_ascii=False, default=str)
            except TypeError:
                tool_result_str = str(tool_result)

            tool_message = {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "name": tool_log.get("tool_name"),
                "content": tool_result_str,
            }

            snapshot_created, _, archived_records = await mysql_connection.async_insert_chat_record(
                conversation_id,
                "tool",
                tool_message,
            )

        if archived_records:
            await send_permanent_records_archive(
                context.bot,
                user_id,
                archived_records,
                logger=logger,
            )
        if snapshot_created:
            summary.schedule_summary_generation(conversation_id)


async def _claim_due_schedules(limit: int = SCHEDULE_BATCH_SIZE) -> list[tuple]:
    async with mysql_connection.transaction() as connection:
        rows = await mysql_connection.fetch_all(
            "SELECT id, user_id, run_at, created_at, trigger_reason, context, prompt "
            "FROM ai_schedules "
            "WHERE status = 'pending' AND run_at <= UTC_TIMESTAMP() "
            "ORDER BY run_at ASC, id ASC "
            "LIMIT %s FOR UPDATE",
            (limit,),
            connection=connection,
        )
        if not rows:
            return []

        schedule_ids = [row[0] for row in rows]
        placeholders = ", ".join(["%s"] * len(schedule_ids))
        await connection.exec_driver_sql(
            f"UPDATE ai_schedules SET status = 'executing' WHERE id IN ({placeholders})",
            tuple(schedule_ids),
        )

    return rows


async def _process_schedule_task(
    task_row: tuple,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    schedule_id, user_id, run_at, created_at, trigger_reason, context_text, instruction = task_row
    if isinstance(trigger_reason, bytes):
        trigger_reason = trigger_reason.decode("utf-8", errors="ignore")
    if isinstance(context_text, bytes):
        context_text = context_text.decode("utf-8", errors="ignore")
    if isinstance(instruction, bytes):
        instruction = instruction.decode("utf-8", errors="ignore")

    try:
        user_state_prompt = await _build_user_state_prompt(user_id)
        if user_state_prompt is None:
            await _mark_schedule_status(
                schedule_id,
                "failed",
                error="user not found",
            )
            return

        now_utc = datetime.utcnow().replace(tzinfo=timezone.utc)
        scheduled_message = _format_scheduled_message(
            timestamp=now_utc,
            scheduled_at=created_at,
            scheduled_for=run_at,
            trigger_reason=trigger_reason or "",
            context_text=context_text or "",
            instruction=instruction or "",
        )

        snapshot_created, _, archived_records = await mysql_connection.async_insert_chat_record(
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
        if snapshot_created:
            summary.schedule_summary_generation(user_id)

        chat_history = await mysql_connection.async_get_chat_history(user_id)
        tool_context = {
            "is_group": False,
            "group_id": None,
            "message_id": None,
            "user_id": user_id,
            "user_state_prompt": user_state_prompt,
        }

        try:
            await context.bot.send_chat_action(chat_id=user_id, action="typing")
        except Exception:
            logger.debug("Failed to send typing action for scheduled task %s", schedule_id)

        assistant_message, tool_logs = await ai_chat.get_ai_response(
            list(chat_history),
            user_id,
            tool_context=tool_context,
        )

        if tool_logs:
            await _persist_tool_logs(user_id, tool_logs, context, user_id)

        if not assistant_message or not str(assistant_message).strip():
            raise RuntimeError("empty assistant response")

        snapshot_created, _, archived_records = await mysql_connection.async_insert_chat_record(
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
        if snapshot_created:
            summary.schedule_summary_generation(user_id)

        send_func = partial_send(context.bot.send_message, user_id)
        for segment in split_ai_reply(str(assistant_message)):
            await safe_send_markdown(
                send_func,
                segment,
                logger=logger,
                fallback_send=send_func,
            )

        await _mark_schedule_status(schedule_id, "executed")
    except Exception as exc:
        logger.exception("Scheduled task %s failed: %s", schedule_id, exc)
        error_text = str(exc)
        if len(error_text) > 500:
            error_text = error_text[:500]
        await _mark_schedule_status(schedule_id, "failed", error=error_text)


async def run_ai_schedule_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    if _schedule_lock.locked():
        return

    async with _schedule_lock:
        tasks = await _claim_due_schedules()
        if not tasks:
            return
        for task in tasks:
            await _process_schedule_task(task, context)
