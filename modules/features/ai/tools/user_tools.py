import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from core import mysql_connection, process_user

from .context import get_tool_request_context


def _get_user_by_id(user_id: int) -> Optional[Dict[str, object]]:
    row = mysql_connection.run_sync(
        mysql_connection.fetch_one(
            "SELECT id, name, coins FROM user WHERE id = %s",
            (user_id,),
        )
    )
    if not row:
        return None
    return {"id": row[0], "name": row[1], "coins": row[2]}


def _get_last_kindness_for_recipient(recipient_id: int) -> Optional[Dict[str, object]]:
    row = mysql_connection.run_sync(
        mysql_connection.fetch_one(
            "SELECT amount, created_at FROM kindness_gifts "
            "WHERE recipient_id = %s ORDER BY created_at DESC LIMIT 1",
            (recipient_id,),
        )
    )
    if not row:
        return None
    return {"amount": row[0], "created_at": row[1]}


def kindness_gift_tool(
    amount: Optional[int] = None,
    **kwargs,
) -> dict:
    context = get_tool_request_context()
    try:
        recipient_id = int(context.get("user_id"))
    except (TypeError, ValueError):
        return {"error": "Missing recipient information, cannot execute gift"}

    recipient = _get_user_by_id(recipient_id)
    if not recipient:
        return {"error": "Recipient user not found"}

    last_record = _get_last_kindness_for_recipient(recipient["id"])
    if last_record and last_record.get("created_at"):
        last_time = last_record["created_at"]
        if last_time.tzinfo is None:
            last_time = last_time.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - last_time < timedelta(hours=24):
            return {
                "status": "cooldown",
                "last_amount": last_record["amount"],
                "last_time": last_time.isoformat(sep=" "),
                "message": "Failed: 24-hour cooldown period has not elapsed. Cannot gift coins again yet",
            }

    try:
        amt = int(amount) if amount is not None else random.randint(1, 10)
    except (TypeError, ValueError):
        amt = random.randint(1, 10)
    amt = max(1, min(amt, 10))

    try:
        async def _record_gift():
            async with mysql_connection.transaction() as connection:
                await connection.exec_driver_sql(
                    "UPDATE user SET coins = coins + %s WHERE id = %s",
                    (amt, recipient["id"]),
                )
                await connection.exec_driver_sql(
                    "INSERT INTO kindness_gifts (recipient_id, amount, created_at) "
                    "VALUES (%s, %s, NOW())",
                    (recipient["id"], amt),
                )

        mysql_connection.run_sync(_record_gift())
    except Exception as exc:
        logging.error("Failed to record kindness gift: %s", exc)
        return {"error": "Error recording gift, please try again later"}

    latest = _get_last_kindness_for_recipient(recipient["id"])
    last_time_str = None
    last_amount = None
    if latest and latest.get("created_at"):
        last_time_value = latest["created_at"]
        if last_time_value.tzinfo is None:
            last_time_value = last_time_value.replace(tzinfo=timezone.utc)
        last_time_str = last_time_value.isoformat(sep=" ")
        last_amount = latest.get("amount")

    return {
        "status": "granted",
        "recipient_id": recipient["id"],
        "recipient_username": f"@{recipient['name']}" if recipient.get("name") else None,
        "amount": amt,
        "last_time": last_time_str,
        "last_amount": last_amount,
        "recipient_coins_before": recipient["coins"],
        "recipient_coins_after": recipient["coins"] + amt,
        "message": f"Successfully gifted {amt} coins to user",
    }


def update_affection_tool(delta: int, **kwargs) -> dict:
    """Adjust the AI's affection towards the current user."""
    context = get_tool_request_context()
    user_id = context.get("user_id")
    if not user_id:
        return {"error": "Missing user information, cannot update affection level"}

    try:
        change = int(delta)
    except (TypeError, ValueError):
        return {"error": "Affection change value must be an integer"}

    if change > 10:
        change = 10
    elif change < -10:
        change = -10

    try:
        affection = process_user.get_user_affection_sync(user_id)
    except Exception as exc:
        logging.exception("Failed to fetch affection: %s", exc)
        return {"error": "Error querying affection level, please try again later"}

    if affection is None:
        return {"error": "User affection data not found"}

    if (affection >= 100 and change > 0) or (affection <= -100 and change < 0):
        return {"error": "Affection level has reached the limit, cannot adjust further"}

    try:
        new_affection = process_user.update_user_affection_sync(user_id, change)
    except Exception as exc:
        logging.exception("Failed to update affection: %s", exc)
        return {"error": "Error updating affection level, please try again later"}

    return {
        "user_id": user_id,
        "change": change,
        "affection": new_affection,
        "message": f"Affection level adjusted by {change:+d}, current value: {new_affection}",
    }


def update_impression_tool(impression: str, **kwargs) -> dict:
    """Write or overwrite the AI's impression of the current user."""
    context = get_tool_request_context()
    user_id = context.get("user_id")
    if not user_id:
        return {"error": "Missing user information, cannot update impression"}

    text = (impression or "").strip()
    if len(text) > 500:
        text = text[:500]

    try:
        saved = process_user.update_user_impression_sync(user_id, text)
    except Exception as exc:
        logging.exception("Failed to update impression: %s", exc)
        return {"error": "Error updating impression"}

    return {
        "user_id": user_id,
        "impression": saved,
        "message": "Impression record updated successfully",
    }


__all__ = [
    "kindness_gift_tool",
    "update_affection_tool",
    "update_impression_tool",
]
