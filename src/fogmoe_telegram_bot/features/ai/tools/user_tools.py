import logging
import random
from dataclasses import dataclass
from datetime import UTC, datetime

from fogmoe_telegram_bot.core import balance, mysql_connection, process_user

from .context import get_tool_request_context

AFFECTION_TOOL_ENABLED = False


KINDNESS_COOLDOWN_HOURS = 24


@dataclass(frozen=True)
class KindnessOutcome:
    """一次善意赠币的结果。`granted` 为 False 表示仍在冷却期，`last_*` 是上一次赠币的记录。"""

    granted: bool
    recipient_name: str | None
    coins_before: int
    amount: int
    last_amount: int | None
    last_time: datetime | None


def kindness_op_key(recipient_id: int, last_gift_time: datetime | None) -> str:
    """一次赠币资格对应的 op_key：由收款人与「上一次赠币时间」决定。

    24 小时冷却的窗口只会随着赠币记录前进，所以同一个窗口里的重试、并发都得到同一个
    op_key，赠币最多入账一次；从没收到过赠币记为 `never`。
    """
    stamp = "never" if last_gift_time is None else last_gift_time.strftime("%Y%m%dT%H%M%S")
    return balance.make_op_key("kindness", recipient_id, stamp)


async def _latest_kindness(connection, recipient_id: int):
    return await mysql_connection.fetch_one(
        "SELECT amount, created_at FROM kindness_gifts "
        "WHERE recipient_id = %s ORDER BY created_at DESC, id DESC LIMIT 1",
        (recipient_id,),
        connection=connection,
    )


async def grant_kindness(recipient_id: int, amount: int) -> KindnessOutcome | None:
    """冷却检查、入账与赠币记录在同一个事务里；收款人不存在返回 None。

    先锁收款人的 user 行，同一个人的并发赠币在这里串行，冷却检查因此不会被抢先。
    冷却期用数据库时钟判断（`created_at` 也是数据库写的），与会话时区无关。
    """

    async def work(connection) -> KindnessOutcome | None:
        try:
            balances = await balance.lock_user(connection, recipient_id)
        except balance.UserNotFound:
            return None
        name_row = await mysql_connection.fetch_one(
            "SELECT name FROM user WHERE id = %s",
            (recipient_id,),
            connection=connection,
        )
        recipient_name = name_row[0] if name_row else None

        # 用户行已经锁住，这是事务里第一次一致性读，看到的是上一个持锁者提交之后的记录。
        last = await mysql_connection.fetch_one(
            "SELECT amount, created_at, "
            "created_at > NOW() - INTERVAL %s HOUR AS cooling FROM kindness_gifts "
            "WHERE recipient_id = %s ORDER BY created_at DESC, id DESC LIMIT 1",
            (KINDNESS_COOLDOWN_HOURS, recipient_id),
            connection=connection,
        )
        last_time = last[1] if last else None
        op_key = kindness_op_key(recipient_id, last_time)
        # 记录被手工清掉但这个窗口的赠币已经入账：按冷却处理，不再入账也不补记录。
        granted_before = await balance.get_operation(op_key, connection=connection)
        if (last and last[2]) or granted_before is not None:
            last_amount = int(last[0]) if last else granted_before.amount
            return KindnessOutcome(
                False, recipient_name, balances.total, 0, last_amount, last_time
            )

        await balance.credit(
            connection, recipient_id, amount, op_key=op_key, reason="kindness"
        )
        await connection.exec_driver_sql(
            "INSERT INTO kindness_gifts (recipient_id, amount, created_at) "
            "VALUES (%s, %s, NOW())",
            (recipient_id, amount),
        )
        latest = await _latest_kindness(connection, recipient_id)
        return KindnessOutcome(
            True,
            recipient_name,
            balances.total,
            amount,
            int(latest[0]) if latest else None,
            latest[1] if latest else None,
        )

    return await balance.run_in_transaction(work)


def _iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.isoformat(sep=" ")


async def kindness_gift_tool(
    amount: int | None = None,
    **kwargs,
) -> dict:
    context = get_tool_request_context()
    try:
        recipient_id = int(context.get("user_id"))
    except (TypeError, ValueError):
        return {"error": "Missing recipient information, cannot execute gift"}

    try:
        amt = int(amount) if amount is not None else random.randint(1, 10)
    except (TypeError, ValueError):
        amt = random.randint(1, 10)
    amt = max(1, min(amt, 10))

    try:
        outcome = await grant_kindness(recipient_id, amt)
    except Exception as exc:
        logging.error("Failed to record kindness gift: %s", exc)
        return {"error": "Error recording gift, please try again later"}

    if outcome is None:
        return {"error": "Recipient user not found"}

    if not outcome.granted:
        last_time = outcome.last_time
        return {
            "status": "cooldown",
            "last_amount": outcome.last_amount,
            "last_time": _iso(last_time) if last_time else None,
            "message": "Failed: 24-hour cooldown period has not elapsed. Cannot gift coins again yet",
        }

    return {
        "status": "granted",
        "recipient_id": recipient_id,
        "recipient_username": f"@{outcome.recipient_name}" if outcome.recipient_name else None,
        "amount": amt,
        "last_time": _iso(outcome.last_time) if outcome.last_time else None,
        "last_amount": outcome.last_amount,
        "recipient_coins_before": outcome.coins_before,
        "recipient_coins_after": outcome.coins_before + amt,
        "message": f"Successfully gifted {amt} coins to user",
    }


async def update_affection_tool(delta: int, **kwargs) -> dict:
    """Adjust the AI's affection towards the current user."""
    if not AFFECTION_TOOL_ENABLED:
        return {"error": "Affection tool is temporarily disabled"}

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
        affection = await process_user.get_user_affection(user_id)
    except Exception as exc:
        logging.exception("Failed to fetch affection: %s", exc)
        return {"error": "Error querying affection level, please try again later"}

    if affection is None:
        return {"error": "User affection data not found"}

    if (affection >= 100 and change > 0) or (affection <= -100 and change < 0):
        return {"error": "Affection level has reached the limit, cannot adjust further"}

    try:
        new_affection = await process_user.update_user_affection(user_id, change)
    except Exception as exc:
        logging.exception("Failed to update affection: %s", exc)
        return {"error": "Error updating affection level, please try again later"}

    return {
        "user_id": user_id,
        "change": change,
        "affection": new_affection,
        "message": f"Affection level adjusted by {change:+d}, current value: {new_affection}",
    }


async def update_impression_tool(impression: str, **kwargs) -> dict:
    """Write or overwrite the AI's impression of the current user."""
    context = get_tool_request_context()
    user_id = context.get("user_id")
    if not user_id:
        return {
            "user_id": None,
            "error": "Missing user information, cannot update impression",
        }

    text = (impression or "").strip()
    if not text:
        return {"user_id": user_id, "error": "Impression text must not be empty"}
    if len(text) > 500:
        text = text[:500]

    try:
        saved = await process_user.update_user_impression(user_id, text)
    except Exception as exc:
        logging.exception("Failed to update impression: %s", exc)
        return {"user_id": user_id, "error": "Error updating impression"}

    return {
        "user_id": user_id,
        "impression": saved,
        "message": "Impression record updated successfully",
    }


__all__ = [
    "kindness_gift_tool",
    "update_impression_tool",
]
