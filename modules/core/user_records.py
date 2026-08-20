"""user 表的基础查询。"""

from .sql import fetch_one


async def check_user_exists(user_id: int) -> bool:
    row = await fetch_one("SELECT id FROM user WHERE id = %s", (user_id,))
    return row is not None


async def async_check_user_exists(user_id: int) -> bool:
    return await check_user_exists(user_id)
