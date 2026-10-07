"""群组 AI 垃圾识别的业务规则：按 30 天付费、暂停与恢复、到期提醒，以及哪些消息交给 AI 检查。不依赖 Telegram。

规则（已确认的产品决定）：

- 每 `PERIOD_DAYS` 天 `PERIOD_PRICE` 金币，由执行命令的管理员支付；到期不自动扣费，管理员手动续费。
  付费在 `max(现在, 原到期时间)` 上加 30 天，可以提前续。扣费之后不退款，暂停期间有效期照常流逝。
- 只检查每位成员在群里的前 `CHECKED_MESSAGES_PER_MEMBER` 条正常消息：被判定为垃圾信息的消息不计数，
  所以发广告的账号会一直被检查；判定失败（超时、限流）的消息放行，也不计数。
- 每个群每天（UTC）最多调用 `DAILY_CHECK_LIMIT` 次，超出后当天只用关键词过滤。

付费的扣款与有效期在同一个事务里提交。`op_key` 以 `/spam ai` 命令消息为身份（`payment_op_key`），
同一条命令被重复投递时扣款重放，不会再延长一次有效期。

检查计数之外的状态（群状态缓存、已满额的成员、每日调用次数）放在进程内存里，重启后从数据库重新读取，
每日调用次数随重启清零。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_telegram_bot.core import balance, sql
from fogmoe_telegram_bot.core.command_identity import message_identity

from .repositories import groups
from .repositories.groups import DueReminder, ReminderKind, SpamAiGroup

PERIOD_PRICE = 100
PERIOD_DAYS = 30
EXPIRING_REMINDER_DAYS = 3
CHECKED_MESSAGES_PER_MEMBER = 5
SPAM_THRESHOLD = 0.8
DAILY_CHECK_LIMIT = 2000
STATUS_CACHE_SECONDS = 300
REMINDER_INTERVAL_SECONDS = 3600
PAYMENT_REASON = "spam_ai"
# 已满额成员的内存记录超过这个数就整体清空，之后按需从数据库重新读取
MAX_EXEMPT_ENTRIES = 200_000


class PayStatus(StrEnum):
    CHARGED = "charged"
    RESUMED = "resumed"
    ALREADY_ON = "already_on"
    NOT_REGISTERED = "not_registered"
    INSUFFICIENT = "insufficient"


@dataclass(frozen=True, slots=True)
class PayResult:
    status: PayStatus
    paid_until: datetime | None = None
    extended: bool = False  # CHARGED：付费前还在有效期内，这次是续期
    resumed: bool = False  # CHARGED：付费前处于暂停，这次一并恢复
    balance_total: int = 0  # INSUFFICIENT：当前余额


def utcnow() -> datetime:
    """与数据库里的时间列可比较的当前时间（UTC，naive）。"""
    return datetime.now(UTC).replace(tzinfo=None)


def payment_op_key(chat_id: int, message_id: int) -> str:
    return balance.make_op_key("spamai", *message_identity(chat_id, message_id))


# ---------------------------------------------------------------------------
# 付费、暂停与状态
# ---------------------------------------------------------------------------


async def _charge(
    connection: AsyncConnection,
    chat_id: int,
    user_id: int,
    op_key: str,
    before: SpamAiGroup | None,
) -> PayResult:
    # 余额不足交给 debit 判断：它先认出同一个 op_key 的重放，再报余额不足，两种情况都没有改动数据。
    try:
        debit = await balance.debit(
            connection,
            user_id,
            PERIOD_PRICE,
            op_key=op_key,
            reason=PAYMENT_REASON,
            ref=f"chat:{chat_id}",
        )
    except balance.UserNotFound:
        return PayResult(PayStatus.NOT_REGISTERED)
    except balance.InsufficientBalance as exc:
        return PayResult(PayStatus.INSUFFICIENT, balance_total=exc.balance_total)
    if debit.applied:
        await groups.extend_period(connection, chat_id, user_id=user_id, days=PERIOD_DAYS)
    after = await groups.get_group(chat_id, connection=connection)
    return PayResult(
        PayStatus.CHARGED,
        paid_until=after.paid_until if after else None,
        extended=before is not None and before.active,
        resumed=before is not None and before.active and not before.enabled,
    )


async def enable(chat_id: int, user_id: int, op_key: str) -> PayResult:
    """开启检查：还在有效期内只恢复，不扣费；从没付过费或已到期时扣一期的费用。"""

    async def work(connection: AsyncConnection) -> PayResult:
        group = await groups.get_group(chat_id, connection=connection, for_update=True)
        if group is not None and group.active:
            if group.enabled:
                return PayResult(PayStatus.ALREADY_ON, paid_until=group.paid_until)
            await groups.resume(connection, chat_id)
            return PayResult(PayStatus.RESUMED, paid_until=group.paid_until)
        return await _charge(connection, chat_id, user_id, op_key, group)

    result = await balance.run_in_transaction(work)
    forget_status(chat_id)
    return result


async def renew(chat_id: int, user_id: int, op_key: str) -> PayResult:
    """续费一期并开启检查；还在有效期内时接在原到期时间之后。"""

    async def work(connection: AsyncConnection) -> PayResult:
        group = await groups.get_group(chat_id, connection=connection, for_update=True)
        return await _charge(connection, chat_id, user_id, op_key, group)

    result = await balance.run_in_transaction(work)
    forget_status(chat_id)
    return result


async def pause(chat_id: int) -> SpamAiGroup | None:
    """暂停检查，返回暂停后的状态；当前没有在检查（没付费、已到期或已暂停）时返回 None。"""
    async with sql.transaction() as connection:
        paused = await groups.pause(connection, chat_id)
        group = await groups.get_group(chat_id, connection=connection) if paused else None
    forget_status(chat_id)
    return group


async def get_status(chat_id: int) -> SpamAiGroup | None:
    return await groups.get_group(chat_id)


async def move_group(old_chat_id: int, new_chat_id: int) -> bool:
    """普通群升级成超级群：付费状态与检查计数搬到新的 chat id。没有可搬的、或新群已经有自己的记录时返回 False。"""
    async with sql.transaction() as connection:
        moved = await groups.move_chat(connection, old_chat_id, new_chat_id)
    forget_status(old_chat_id)
    forget_status(new_chat_id)
    return moved


# 消息路径上用的群状态缓存：{chat_id: (读取时刻, 状态)}
_status_cache: dict[int, tuple[float, SpamAiGroup | None]] = {}


def forget_status(chat_id: int) -> None:
    _status_cache.pop(chat_id, None)


async def is_checking(chat_id: int) -> bool:
    """这个群现在要不要做 AI 检查：付过费、没暂停、没到期。状态缓存 `STATUS_CACHE_SECONDS` 秒。"""
    cached = _status_cache.get(chat_id)
    if cached is not None and time.monotonic() - cached[0] < STATUS_CACHE_SECONDS:
        group = cached[1]
    else:
        group = await groups.get_group(chat_id)
        _status_cache[chat_id] = (time.monotonic(), group)
    return group is not None and group.enabled and group.paid_until > utcnow()


# ---------------------------------------------------------------------------
# 哪些消息交给 AI，结果怎么记
# ---------------------------------------------------------------------------

# 已经满额、不再检查的成员
_exempt: set[tuple[int, int]] = set()
# 每个群当天的调用次数：{chat_id: (UTC 日期, 次数)}
_daily_checks: dict[int, tuple[date, int]] = {}


def _mark_exempt(chat_id: int, user_id: int) -> None:
    if len(_exempt) >= MAX_EXEMPT_ENTRIES:
        _exempt.clear()
    _exempt.add((chat_id, user_id))


def is_exempt(chat_id: int, user_id: int) -> bool:
    """内存里记着已经满额的成员；不在里面的要再查数据库（`needs_review`）。"""
    return (chat_id, user_id) in _exempt


async def needs_review(chat_id: int, user_id: int) -> bool:
    if is_exempt(chat_id, user_id):
        return False
    if await groups.get_checked_count(chat_id, user_id) >= CHECKED_MESSAGES_PER_MEMBER:
        _mark_exempt(chat_id, user_id)
        return False
    return True


def _today() -> date:
    return datetime.now(UTC).date()


def take_daily_check(chat_id: int, today: date | None = None) -> bool:
    """占用一次当天的调用额度；额度用完返回 False。"""
    today = today or _today()
    day, used = _daily_checks.get(chat_id, (today, 0))
    if day != today:
        used = 0
    if used >= DAILY_CHECK_LIMIT:
        return False
    _daily_checks[chat_id] = (today, used + 1)
    return True


def daily_limit_reached(chat_id: int, today: date | None = None) -> bool:
    today = today or _today()
    day, used = _daily_checks.get(chat_id, (today, 0))
    return day == today and used >= DAILY_CHECK_LIMIT


def is_spam(probability: float) -> bool:
    return probability >= SPAM_THRESHOLD


async def record_clean(chat_id: int, user_id: int) -> int:
    """成员又有一条消息被判定为正常，返回累计条数；满额后不再检查这个人。"""
    async with sql.transaction() as connection:
        count = await groups.add_checked(connection, chat_id, user_id)
    if count >= CHECKED_MESSAGES_PER_MEMBER:
        _mark_exempt(chat_id, user_id)
    return count


async def forget_member(chat_id: int, user_id: int) -> None:
    """成员被移出群：清掉检查计数，冷却结束后再进群会重新检查。"""
    _exempt.discard((chat_id, user_id))
    async with sql.transaction() as connection:
        await groups.forget_member(connection, chat_id, user_id)


# ---------------------------------------------------------------------------
# 到期提醒
# ---------------------------------------------------------------------------


async def due_reminders(kind: ReminderKind) -> list[DueReminder]:
    return await groups.list_due_reminders(kind, within_days=EXPIRING_REMINDER_DAYS)


async def claim_reminder(kind: ReminderKind, reminder: DueReminder) -> bool:
    """先记下再发送：同一次提醒最多发一次，发送失败也不重发。"""
    async with sql.transaction() as connection:
        return await groups.claim_reminder(connection, kind, reminder.chat_id, reminder.paid_until)
