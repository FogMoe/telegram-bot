"""用户状态提示词：余额、套餐、权限、印象、个人信息与日记是否存在，每次请求拼进 system prompt。

一轮 AI 对话的三个入口都在这里拼：用户消息的余额、权限与个人信息来自扣费事务（`user_state_prompt`），
定时任务与空闲跟进自己读 user 表（`build_user_state_prompt`）。印象与日记两项都由 `load_user_state_record` 读取。
"""

from dataclasses import dataclass

from fogmoe_telegram_bot.core import balance, mysql_connection, process_user
from fogmoe_telegram_bot.core.prompt_utils import format_user_state_prompt


@dataclass(frozen=True, slots=True)
class UserStateRecord:
    """拼用户状态提示词需要、但不在 user 表里的两项读取：印象与日记是否存在。"""

    impression: str | None
    diary_exists: bool


def format_impression(raw: str | None) -> str:
    """印象文本：压成单行并截到 500 字符；没有记录时是 `Not recorded`。"""
    text = (raw or "").strip()
    if not text:
        return "Not recorded"
    text = text.replace("\r", " ").replace("\n", " ")
    if len(text) > 500:
        text = text[:497] + "..."
    return text


def format_personal_info(raw: str | None) -> str:
    return (raw or "").strip()[:500]


async def load_user_state_record(user_id: int) -> UserStateRecord:
    impression = await process_user.async_get_user_impression(user_id)
    diary_row = await mysql_connection.fetch_one(
        "SELECT 1 FROM ai_user_diary_pages WHERE user_id = %s AND content != '' LIMIT 1",
        (user_id,),
    )
    return UserStateRecord(impression=impression, diary_exists=bool(diary_row))


def user_state_prompt(
    *,
    user_id: int,
    coins_free: int,
    coins_paid: int,
    permission: int,
    info: str | None,
    record: UserStateRecord,
) -> str:
    return format_user_state_prompt(
        user_coins=coins_free + coins_paid,
        user_plan=balance.resolve_user_plan(user_id, coins_paid),
        user_permission=permission,
        impression=format_impression(record.impression),
        personal_info=format_personal_info(info),
        diary_exists=record.diary_exists,
    )


async def build_user_state_prompt(user_id: int) -> str | None:
    """定时任务与空闲跟进用：自己读 user 表；用户不存在返回 None。"""
    row = await mysql_connection.fetch_one(
        "SELECT permission, coins, coins_paid, info FROM user WHERE id = %s",
        (user_id,),
    )
    if not row:
        return None
    return user_state_prompt(
        user_id=user_id,
        coins_free=row[1] or 0,
        coins_paid=row[2] or 0,
        permission=row[0],
        info=row[3],
        record=await load_user_state_record(user_id),
    )


__all__ = [
    "UserStateRecord",
    "build_user_state_prompt",
    "format_impression",
    "format_personal_info",
    "load_user_state_record",
    "user_state_prompt",
]
