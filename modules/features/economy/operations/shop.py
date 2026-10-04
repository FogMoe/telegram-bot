"""商店购买的业务操作：每种商品一个类型化的请求与结果，不依赖 Telegram。

每次购买是一个事务：扣款、发放（权限、记忆上限、彩票奖励与保底奖励）一起提交，任何一步失败整体回滚，
所以不需要退款。`op_key` 以按钮回调的 query id 为身份（`shop_op_key`）：同一次点击被重复投递
只会读回第一次的结果，不会重复扣款与发放；两次不同的点击是两次购买。余额不足发生在任何写入之前。

保底计数是进程内状态（`scratch_records` / `huanle_records`），只在事务提交之后才由本模块写回，
所以回滚不会留下半个计数。进程内的并发由调用方（适配层的购买锁）串行，数据库层的串行靠 user 行锁。

契约见 docs/balance-service.md 与 docs/architecture.md。
"""

import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import TypedDict

from sqlalchemy.ext.asyncio import AsyncConnection

from core import balance, user_records

from ..repositories import shop as shop_repository

MEMORY_LIMIT_PRICE = 100
# 权限等级 -> 升级到该等级的价格
PERMISSION_UPGRADE_PRICES = {1: 50, 2: 100, 3: 10000}

SCRATCH_PRICE = 10
SCRATCH_PITY_THRESHOLD = 5
SCRATCH_PITY_BONUS = 10
HUANLE_PRICE = 1
HUANLE_PITY_THRESHOLD = 5
HUANLE_PITY_BONUS = 2


class PurchaseStatus(StrEnum):
    PURCHASED = "purchased"
    NOT_REGISTERED = "not_registered"
    INSUFFICIENT = "insufficient"
    NOT_ELIGIBLE = "not_eligible"  # 仅权限升级：当前权限不允许买这一级


def shop_op_key(item: str, query_id: object) -> str:
    """商店购买的 op_key：以按钮回调的 query id 为身份，同一次点击被重复投递不会再扣一次。

    没有 query id（不应该发生）时退回一次性 op_key，此时没有重放保护。
    """
    if not query_id:
        return balance.new_op_key(f"shop:{item}")
    return balance.make_op_key("shop", item, query_id)


# ---------------------------------------------------------------------------
# 永久记忆上限 +1
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MemoryLimitPurchase:
    user_id: int
    op_key: str


@dataclass(frozen=True, slots=True)
class MemoryLimitResult:
    status: PurchaseStatus
    new_limit: int | None = None  # PURCHASED：购买后的永久记忆上限（重放时是当前值）


async def buy_memory_limit(request: MemoryLimitPurchase) -> MemoryLimitResult:
    """购买永久记忆上限 +1：扣款与上限更新同一个事务。"""

    async def work(connection: AsyncConnection) -> MemoryLimitResult:
        try:
            balances = await balance.lock_user(connection, request.user_id)
        except balance.UserNotFound:
            return MemoryLimitResult(PurchaseStatus.NOT_REGISTERED)
        if balances.total < MEMORY_LIMIT_PRICE:
            return MemoryLimitResult(PurchaseStatus.INSUFFICIENT)
        try:
            result = await balance.debit(
                connection,
                request.user_id,
                MEMORY_LIMIT_PRICE,
                op_key=request.op_key,
                reason="shop_memory",
            )
        except balance.InsufficientBalance:
            return MemoryLimitResult(PurchaseStatus.INSUFFICIENT)
        if result.applied:
            await shop_repository.increase_permanent_records_limit(connection, request.user_id, 1)
        new_limit = await shop_repository.get_permanent_records_limit(connection, request.user_id)
        return MemoryLimitResult(PurchaseStatus.PURCHASED, new_limit)

    return await balance.run_in_transaction(work)


# ---------------------------------------------------------------------------
# 权限升级
# ---------------------------------------------------------------------------


class UpgradeRefusal(StrEnum):
    """当前权限不允许升级到目标等级的原因。"""

    ALREADY_UPGRADED = "already_upgraded"  # 目标 1 级，但当前权限不是 0
    NEED_LEVEL_1 = "need_level_1"
    HAS_LEVEL_2 = "has_level_2"
    NEED_LEVEL_2 = "need_level_2"
    HAS_LEVEL_3 = "has_level_3"


def permission_upgrade_refusal(current_permission: int, target_level: int) -> UpgradeRefusal | None:
    """当前权限不允许升级到 `target_level` 时返回原因，允许返回 None。"""
    if target_level == 1:
        if current_permission != 0:
            return UpgradeRefusal.ALREADY_UPGRADED
    elif target_level == 2:
        if current_permission == 0:
            return UpgradeRefusal.NEED_LEVEL_1
        if current_permission >= 2:
            return UpgradeRefusal.HAS_LEVEL_2
    elif target_level == 3:
        if current_permission < 2:
            return UpgradeRefusal.NEED_LEVEL_2
        if current_permission >= 3:
            return UpgradeRefusal.HAS_LEVEL_3
    return None


@dataclass(frozen=True, slots=True)
class PermissionUpgrade:
    user_id: int
    target_level: int
    op_key: str


@dataclass(frozen=True, slots=True)
class PermissionUpgradeResult:
    status: PurchaseStatus
    level: int = 0  # PURCHASED：升级后的权限等级
    refusal: UpgradeRefusal | None = None  # NOT_ELIGIBLE：不允许的原因


async def upgrade_permission(request: PermissionUpgrade) -> PermissionUpgradeResult:
    """购买权限升级到 `request.target_level` 级：扣款与权限更新同一个事务。"""
    price = PERMISSION_UPGRADE_PRICES[request.target_level]

    async def work(connection: AsyncConnection) -> PermissionUpgradeResult:
        try:
            balances = await balance.lock_user(connection, request.user_id)
        except balance.UserNotFound:
            return PermissionUpgradeResult(PurchaseStatus.NOT_REGISTERED)
        # 用户行已锁住，这是事务里第一次一致性读，看到的是上一个持锁者提交之后的权限。
        current_permission = (
            await user_records.get_permission(request.user_id, connection=connection) or 0
        )
        refusal = permission_upgrade_refusal(current_permission, request.target_level)
        if refusal is not None:
            return PermissionUpgradeResult(PurchaseStatus.NOT_ELIGIBLE, refusal=refusal)
        if balances.total < price:
            return PermissionUpgradeResult(PurchaseStatus.INSUFFICIENT)
        try:
            result = await balance.debit(
                connection,
                request.user_id,
                price,
                op_key=request.op_key,
                reason="shop_permission",
            )
        except balance.InsufficientBalance:
            return PermissionUpgradeResult(PurchaseStatus.INSUFFICIENT)
        if result.applied:
            await shop_repository.set_permission(connection, request.user_id, request.target_level)
        return PermissionUpgradeResult(PurchaseStatus.PURCHASED, level=request.target_level)

    return await balance.run_in_transaction(work)


# ---------------------------------------------------------------------------
# 彩票（刮刮乐、欢乐彩）
# ---------------------------------------------------------------------------


class PityRecord(TypedDict):
    """保底记录：同一天内累计的连续「没中」次数与最后一次的日期。"""

    count: int
    date: date


# 进程内的保底记录 {user_id: 记录}；只在购买事务提交之后更新。
scratch_records: dict[int, PityRecord] = {}
huanle_records: dict[int, PityRecord] = {}


def draw_scratch_reward(rng: random.Random | None = None) -> int:
    """刮刮乐：0～20 金币均匀分布。"""
    return (rng or random).randint(0, 20)


def draw_huanle_reward(rng: random.Random | None = None) -> int:
    """欢乐彩：0 金币 80%，1 金币 19%，5 金币 0.95%，100 金币 0.05%。"""
    p = (rng or random).random()
    if p < 0.80:
        return 0
    if p < 0.80 + 0.19:
        return 1
    if p < 0.80 + 0.19 + 0.0095:
        return 5
    return 100


def advance_pity(
    record: PityRecord | None,
    *,
    today: date,
    miss: bool,
    threshold: int,
) -> tuple[PityRecord, bool]:
    """保底计数前进一步，返回 (新的记录, 本次是否触发保底奖励)。

    连续「没中」达到 `threshold` 次（同一天内累计，隔天从头算）触发一次保底，随后计数清零。
    纯函数：只有购买事务提交之后才把新记录写回进程内的字典。
    """
    count = record["count"] if record and record["date"] == today else 0
    count = count + 1 if miss else 0
    if count >= threshold:
        return {"count": 0, "date": today}, True
    return {"count": count, "date": today}, False


@dataclass(frozen=True, slots=True)
class TicketPurchase:
    user_id: int
    op_key: str


@dataclass(frozen=True, slots=True)
class TicketResult:
    """一次购彩的结果。

    PURCHASED 时 `reward` / `bonus` 是开奖与保底奖励；同一次点击被重复投递（重放）时它们是第一次
    记录的值，`pity` 为 None。INSUFFICIENT 时 `balance_total` 是当前余额。
    """

    status: PurchaseStatus
    reward: int = 0
    bonus: int = 0
    balance_total: int = 0
    pity: PityRecord | None = None  # 事务提交后要写回的保底记录；重放时为 None


@dataclass(frozen=True, slots=True)
class _TicketGame:
    item: str
    price: int
    pity_records: dict[int, PityRecord]
    pity_threshold: int
    pity_bonus: int
    draw_reward: Callable[[], int]
    is_miss: Callable[[int], bool]


async def _recorded_credit(connection: AsyncConnection, op_key: str) -> int:
    existing = await balance.get_operation(op_key, connection=connection)
    return existing.amount if existing else 0


async def _buy_ticket(request: TicketPurchase, game: _TicketGame, today: date) -> TicketResult:
    """购买一张彩票：扣款、开奖入账、保底奖励在同一个事务里。

    同一次点击被重复投递（op_key 的扣款是重放）时，奖励已经随第一次事务提交，
    这里只读回当时的结果，不再开奖。
    """

    async def work(connection: AsyncConnection) -> TicketResult:
        try:
            balances = await balance.lock_user(connection, request.user_id)
        except balance.UserNotFound:
            return TicketResult(PurchaseStatus.NOT_REGISTERED)
        if balances.total < game.price:
            return TicketResult(PurchaseStatus.INSUFFICIENT, balance_total=balances.total)

        reward = game.draw_reward()
        try:
            debit = await balance.debit(
                connection,
                request.user_id,
                game.price,
                op_key=request.op_key,
                reason=f"shop_{game.item}",
            )
        except balance.InsufficientBalance as exc:
            return TicketResult(PurchaseStatus.INSUFFICIENT, balance_total=exc.balance_total)

        win_key = f"{request.op_key}:win"
        bonus_key = f"{request.op_key}:bonus"
        if not debit.applied:
            return TicketResult(
                PurchaseStatus.PURCHASED,
                reward=await _recorded_credit(connection, win_key),
                bonus=await _recorded_credit(connection, bonus_key),
            )

        if reward > 0:
            await balance.credit(
                connection,
                request.user_id,
                reward,
                op_key=win_key,
                reason=f"shop_{game.item}_win",
            )
        pity, triggered = advance_pity(
            game.pity_records.get(request.user_id),
            today=today,
            miss=game.is_miss(reward),
            threshold=game.pity_threshold,
        )
        bonus = 0
        if triggered:
            bonus = game.pity_bonus
            await balance.credit(
                connection,
                request.user_id,
                bonus,
                op_key=bonus_key,
                reason=f"shop_{game.item}_bonus",
            )
        return TicketResult(PurchaseStatus.PURCHASED, reward=reward, bonus=bonus, pity=pity)

    purchase = await balance.run_in_transaction(work)
    if purchase.status is PurchaseStatus.PURCHASED and purchase.pity is not None:
        game.pity_records[request.user_id] = purchase.pity
    return purchase


async def buy_scratch_ticket(request: TicketPurchase, *, today: date | None = None) -> TicketResult:
    """刮刮乐：10 金币一张，奖励 0～20；连续 5 次不足 10 金币触发 10 金币保底。"""
    game = _TicketGame(
        item="scratch",
        price=SCRATCH_PRICE,
        pity_records=scratch_records,
        pity_threshold=SCRATCH_PITY_THRESHOLD,
        pity_bonus=SCRATCH_PITY_BONUS,
        draw_reward=draw_scratch_reward,
        is_miss=lambda reward: reward < 10,
    )
    return await _buy_ticket(request, game, today or date.today())


async def buy_huanle_ticket(request: TicketPurchase, *, today: date | None = None) -> TicketResult:
    """欢乐彩：1 金币一张；连续 5 次 0 金币触发 2 金币保底。"""
    game = _TicketGame(
        item="huanle",
        price=HUANLE_PRICE,
        pity_records=huanle_records,
        pity_threshold=HUANLE_PITY_THRESHOLD,
        pity_bonus=HUANLE_PITY_BONUS,
        draw_reward=draw_huanle_reward,
        is_miss=lambda reward: reward == 0,
    )
    return await _buy_ticket(request, game, today or date.today())
