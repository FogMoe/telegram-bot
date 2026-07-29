from typing import Optional

from core import mysql_connection, process_user
from core.prompt_utils import format_user_state_prompt


async def build_user_state_prompt(user_id: int) -> Optional[str]:
    row = await mysql_connection.fetch_one(
        "SELECT permission, coins, coins_paid, info FROM user WHERE id = %s",
        (user_id,),
    )
    if not row:
        return None

    user_permission = row[0]
    user_coins_free = row[1] or 0
    user_coins_paid = row[2] or 0
    user_info_raw = row[3] if len(row) > 3 else ""
    user_coins = user_coins_free + user_coins_paid
    user_plan = process_user.resolve_user_plan(user_id, user_coins_paid)

    user_impression_raw = await process_user.async_get_user_impression(user_id)
    impression_display = (user_impression_raw or "").strip()
    if impression_display:
        impression_display = impression_display.replace("\r", " ").replace("\n", " ")
        if len(impression_display) > 500:
            impression_display = impression_display[:497] + "..."
    else:
        impression_display = "Not recorded"

    personal_info_display = (user_info_raw or "").strip()
    if personal_info_display and len(personal_info_display) > 500:
        personal_info_display = personal_info_display[:500]

    diary_row = await mysql_connection.fetch_one(
        "SELECT 1 FROM ai_user_diary_pages WHERE user_id = %s AND content != '' LIMIT 1",
        (user_id,),
    )

    return format_user_state_prompt(
        user_coins=user_coins,
        user_plan=user_plan,
        user_permission=user_permission,
        impression=impression_display,
        personal_info=personal_info_display,
        diary_exists=bool(diary_row),
    )


__all__ = ["build_user_state_prompt"]
