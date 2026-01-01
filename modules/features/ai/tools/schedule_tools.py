from datetime import datetime, timezone
from typing import Optional

from core import mysql_connection

from .context import get_tool_request_context

MAX_PENDING_SCHEDULES = 3
MAX_TOTAL_SCHEDULES = 12


def _parse_timestamp_utc(value: str | None) -> Optional[datetime]:
    if not value:
        return None

    raw = str(value).strip()
    if not raw:
        return None

    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"

    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                dt = datetime.strptime(raw, fmt)
                break
            except ValueError:
                dt = None
        if dt is None:
            return None

    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _format_timestamp_utc(value: Optional[datetime]) -> Optional[str]:
    if not value:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


async def _create_or_replace_schedule(
    user_id: int,
    run_at: datetime,
    trigger_reason: str,
    context_text: Optional[str],
    instruction_text: str,
) -> tuple[Optional[int], Optional[datetime], bool, Optional[str]]:
    replaced = False
    blocked_reason: Optional[str] = None
    async with mysql_connection.transaction() as connection:
        pending_row = await mysql_connection.fetch_one(
            "SELECT COUNT(*) FROM ai_schedules WHERE user_id = %s AND status = 'pending'",
            (user_id,),
            connection=connection,
        )
        pending_count = pending_row[0] if pending_row and pending_row[0] is not None else 0
        if pending_count >= MAX_PENDING_SCHEDULES:
            return None, None, False, "pending_limit"

        total_row = await mysql_connection.fetch_one(
            "SELECT COUNT(*) FROM ai_schedules WHERE user_id = %s",
            (user_id,),
            connection=connection,
        )
        total_count = total_row[0] if total_row and total_row[0] is not None else 0

        schedule_id = None
        if total_count >= MAX_TOTAL_SCHEDULES:
            row = await mysql_connection.fetch_one(
                "SELECT id FROM ai_schedules WHERE user_id = %s AND status != 'pending' "
                "ORDER BY created_at ASC, id ASC LIMIT 1",
                (user_id,),
                connection=connection,
            )
            schedule_id = row[0] if row else None
            if schedule_id is not None:
                await connection.exec_driver_sql(
                    "UPDATE ai_schedules "
                    "SET run_at = %s, trigger_reason = %s, context = %s, prompt = %s, "
                    "status = 'pending', created_at = UTC_TIMESTAMP(), updated_at = UTC_TIMESTAMP(), "
                    "executed_at = NULL, error = NULL "
                    "WHERE id = %s",
                    (run_at, trigger_reason, context_text, instruction_text, schedule_id),
                )
                replaced = True
            else:
                blocked_reason = "total_limit"
        if schedule_id is None and blocked_reason:
            return None, None, False, blocked_reason
        if schedule_id is None:
            await connection.exec_driver_sql(
                "INSERT INTO ai_schedules (user_id, run_at, trigger_reason, context, prompt) "
                "VALUES (%s, %s, %s, %s, %s)",
                (user_id, run_at, trigger_reason, context_text, instruction_text),
            )
            row = await mysql_connection.fetch_one(
                "SELECT LAST_INSERT_ID()",
                connection=connection,
            )
            schedule_id = row[0] if row and row[0] is not None else None

        created_at = None
        if schedule_id is not None:
            created_row = await mysql_connection.fetch_one(
                "SELECT created_at FROM ai_schedules WHERE id = %s",
                (schedule_id,),
                connection=connection,
            )
            if created_row:
                created_at = created_row[0]

    return schedule_id, created_at, replaced, None


def schedule_ai_message_tool(
    action: Optional[str] = None,
    timestamp_utc: Optional[str] = None,
    trigger_reason: Optional[str] = None,
    context: Optional[str] = None,
    instruction: Optional[str] = None,
    schedule_id: Optional[int] = None,
    **kwargs,
) -> dict:
    request_context = get_tool_request_context()
    user_id = request_context.get("user_id")
    if not user_id:
        return {"user_id": None, "error": "Missing user information, cannot schedule message"}

    action_value = (action or "create").strip().lower()
    if action_value in {"create", "new", "add"}:
        action_value = "create"
    elif action_value in {"list", "ls", "show"}:
        action_value = "list"
    elif action_value in {"cancel", "delete", "remove"}:
        action_value = "cancel"
    else:
        return {"user_id": user_id, "error": f"Unknown action: {action}"}

    warnings: list[str] = []

    if action_value == "list":
        if any([timestamp_utc, trigger_reason, context, instruction, schedule_id]):
            warnings.append("extra fields ignored for list action")

        rows = mysql_connection.run_sync(
            mysql_connection.fetch_all(
                "SELECT id, run_at, created_at, executed_at, status, trigger_reason, context, prompt, error "
                "FROM ai_schedules WHERE user_id = %s "
                "ORDER BY created_at DESC, id DESC LIMIT %s",
                (user_id, MAX_TOTAL_SCHEDULES),
            )
        )
        tasks = []
        pending_count = 0
        for row in rows:
            (
                task_id,
                run_at,
                created_at,
                executed_at,
                status,
                reason,
                context_text,
                instruction_text,
                error_text,
            ) = row
            if status == "pending":
                pending_count += 1
            task = {
                "schedule_id": task_id,
                "timestamp_utc": _format_timestamp_utc(run_at),
                "created_at": _format_timestamp_utc(created_at),
                "status": status,
                "trigger_reason": reason,
                "context": context_text,
                "instruction": instruction_text,
            }
            if executed_at:
                task["executed_at"] = _format_timestamp_utc(executed_at)
            if error_text:
                task["error"] = error_text
            tasks.append(task)

        response = {
            "status": "ok",
            "total": len(tasks),
            "pending_count": pending_count,
            "tasks": tasks,
        }
        if warnings:
            response["warning"] = "; ".join(warnings)
        return response

    if action_value == "cancel":
        if schedule_id is None:
            return {"user_id": user_id, "error": "Missing schedule_id for cancel action"}
        try:
            schedule_id_value = int(schedule_id)
        except (TypeError, ValueError):
            return {"user_id": user_id, "error": "Invalid schedule_id"}

        if any([timestamp_utc, trigger_reason, context, instruction]):
            warnings.append("extra fields ignored for cancel action")

        rowcount = mysql_connection.run_sync(
            mysql_connection.execute(
                "UPDATE ai_schedules SET status = 'cancelled' "
                "WHERE id = %s AND user_id = %s AND status = 'pending'",
                (schedule_id_value, user_id),
            )
        )
        if rowcount == 0:
            return {
                "user_id": user_id,
                "error": "Schedule not found or not pending",
            }

        response = {
            "status": "cancelled",
            "schedule_id": schedule_id_value,
        }
        if warnings:
            response["warning"] = "; ".join(warnings)
        return response

    if not timestamp_utc:
        return {"user_id": user_id, "error": "Missing timestamp_utc for create action"}
    if not trigger_reason:
        return {"user_id": user_id, "error": "Missing trigger_reason for create action"}
    instruction_value = instruction
    if not instruction_value:
        return {"user_id": user_id, "error": "Missing instruction for create action"}

    run_at = _parse_timestamp_utc(timestamp_utc)
    if run_at is None:
        return {"user_id": user_id, "error": "Invalid timestamp_utc format"}

    trigger_reason_value = str(trigger_reason).strip()
    if not trigger_reason_value:
        return {"user_id": user_id, "error": "Empty trigger_reason is not allowed"}
    if len(trigger_reason_value) > 200:
        return {"user_id": user_id, "error": "trigger_reason exceeds 200 characters"}

    instruction_value = str(instruction_value).strip()
    if not instruction_value:
        return {"user_id": user_id, "error": "Empty instruction is not allowed"}
    if len(instruction_value) > 2000:
        return {"user_id": user_id, "error": "instruction exceeds 2000 characters"}

    context_value = None
    if context is not None:
        context_value = str(context).strip()
        if not context_value:
            context_value = None
        elif len(context_value) > 1000:
            return {"user_id": user_id, "error": "context exceeds 1000 characters"}

    schedule_id, created_at, replaced, blocked_reason = mysql_connection.run_sync(
        _create_or_replace_schedule(
            user_id,
            run_at,
            trigger_reason_value,
            context_value,
            instruction_value,
        )
    )
    if schedule_id is None:
        if blocked_reason == "total_limit":
            return {
                "user_id": user_id,
                "error": (
                    f"Too many schedules (max {MAX_TOTAL_SCHEDULES}). "
                    "No non-pending schedule available to overwrite."
                ),
            }
        return {
            "user_id": user_id,
            "error": (
                f"Too many pending schedules (max {MAX_PENDING_SCHEDULES}). "
                "Cancel or wait for execution before creating new ones."
            ),
        }

    response = {
        "status": "scheduled",
        "schedule_id": schedule_id,
        "timestamp_utc": _format_timestamp_utc(run_at),
        "created_at": _format_timestamp_utc(created_at),
        "trigger_reason": trigger_reason_value,
        "replaced_oldest": replaced,
        "instruction": instruction_value,
    }
    if context_value:
        response["context"] = context_value
    return response


__all__ = ["schedule_ai_message_tool"]
