from core import mysql_connection

DAILY_SCHEDULE_TRIGGER_LIMIT = 24


async def reserve_daily_schedule_trigger(user_id: int) -> bool:
    updated_rows = await mysql_connection.execute(
        "UPDATE user SET "
        "ai_schedule_trigger_count = "
        "IF(ai_schedule_trigger_date = UTC_DATE(), ai_schedule_trigger_count + 1, 1), "
        "ai_schedule_trigger_date = UTC_DATE() "
        "WHERE id = %s AND ("
        "ai_schedule_trigger_date IS NULL "
        "OR ai_schedule_trigger_date <> UTC_DATE() "
        "OR ai_schedule_trigger_count < %s) ",
        (user_id, DAILY_SCHEDULE_TRIGGER_LIMIT),
    )
    return updated_rows > 0


__all__ = ["DAILY_SCHEDULE_TRIGGER_LIMIT", "reserve_daily_schedule_trigger"]
