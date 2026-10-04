"""充值的业务操作：卡密兑换、管理员充值请求的决定、卡密生成、/recharge 的禁用截止时间。不依赖 Telegram。

事务都在这里持有：

- 兑换卡密：锁卡密行 -> 入账（以卡密行 id 为 op_key）-> 标记已使用，同一个事务；同一张卡密最多入账一次。
  进程内另有一把「正在处理」的卡密锁，挡住同一进程里同一张卡密的并发请求。
- 充值请求的决定：`UPDATE ... WHERE status = 'pending'` 占住转换，影响行数为 1 才继续（approve 以
  `topup:<id>` 入账付费金币，block 写入禁用截止时间），入账失败时状态保持 pending。
  两个并发的 approve 只有一个能占住转换。

op_key 见 docs/balance-service.md。
"""

import logging
import re
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from threading import RLock

from core import balance, sql
from core.redaction import log_exception

from ..repositories import charge as charge_repository
from ..repositories.charge import TopupRequest

logger = logging.getLogger(__name__)

# 防止同一卡密被并发使用的进程内锁
code_locks: dict[str, bool] = {}
code_lock_mutex = RLock()  # 控制对 code_locks 字典的访问

# UUID格式的正则表达式
UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


def is_valid_uuid(code: str) -> bool:
    """验证字符串是否为有效的UUID格式"""
    return bool(UUID_PATTERN.match(code))


# ---------------------------------------------------------------------------
# 卡密兑换
# ---------------------------------------------------------------------------


class RedeemStatus(StrEnum):
    REDEEMED = "redeemed"
    INVALID_FORMAT = "invalid_format"
    BUSY = "busy"  # 同一张卡密正在被处理
    NOT_FOUND = "not_found"
    ALREADY_USED = "already_used"
    NOT_REGISTERED = "not_registered"
    FAILED = "failed"  # 意外错误；细节只在日志里，`error_ref` 是日志里的引用编号


@dataclass(frozen=True, slots=True)
class RedeemResult:
    status: RedeemStatus
    amount: int = 0  # REDEEMED：到账的付费金币
    used_at: datetime | None = None  # ALREADY_USED：使用时间
    used_by_self: bool = False  # ALREADY_USED：是否是同一个用户用的
    error_ref: str = ""  # FAILED


async def redeem_code(user_id: int, code: str) -> RedeemResult:
    """验证卡密并使用，确保原子操作。"""
    # 验证UUID格式
    if not is_valid_uuid(code):
        return RedeemResult(RedeemStatus.INVALID_FORMAT)

    # 先获取锁，防止同一卡密被并发请求使用
    with code_lock_mutex:
        if code in code_locks:
            return RedeemResult(RedeemStatus.BUSY)
        code_locks[code] = True

    try:
        async with sql.transaction() as connection:
            record = await charge_repository.lock_redemption_code(connection, code)
            if record is None:
                return RedeemResult(RedeemStatus.NOT_FOUND)

            if record.is_used:
                return RedeemResult(
                    RedeemStatus.ALREADY_USED,
                    used_at=record.used_at,
                    used_by_self=record.used_by == user_id,
                )

            # 卡密行已被 FOR UPDATE 锁住，op_key 以卡密行 id 为身份：同一张卡密最多入账一次。
            # 先入账再标记已使用：用户不存在时在这里得到明确的 UserNotFound，而不是外键错误。
            await balance.credit(
                connection,
                user_id,
                record.amount,
                op_key=balance.make_op_key("redeem", record.id),
                reason="redeem_code",
                kind=balance.CoinKind.PAID,
            )
            await charge_repository.mark_code_used(
                connection, record.id, user_id, datetime.now()
            )

        return RedeemResult(RedeemStatus.REDEEMED, amount=record.amount)
    except balance.UserNotFound:
        return RedeemResult(RedeemStatus.NOT_REGISTERED)
    except Exception as e:
        error_ref = log_exception(logger, "充值卡密处理错误", e, extra_secrets=(code,))
        return RedeemResult(RedeemStatus.FAILED, error_ref=error_ref)
    finally:
        # 无论成功与否，都释放锁
        with code_lock_mutex:
            if code in code_locks:
                del code_locks[code]


# ---------------------------------------------------------------------------
# 充值请求
# ---------------------------------------------------------------------------


class TopupAction(StrEnum):
    """管理员对充值请求的按钮动作。"""

    APPROVE = "approve"
    REJECT = "reject"
    BLOCK = "block"


# 动作 -> 请求的目标状态
TOPUP_ACTION_STATUS = {
    TopupAction.APPROVE: "approved",
    TopupAction.REJECT: "rejected",
    TopupAction.BLOCK: "blocked",
}


class DecisionOutcome(StrEnum):
    APPLIED = "applied"  # 这次调用完成了 pending -> 目标状态的转换（approve 同时已入账）
    ALREADY_DECIDED = "already_decided"  # 请求已被处理过，`request.status` 是当前状态，没有任何改动
    NOT_FOUND = "not_found"  # 没有这个请求
    USER_MISSING = "user_missing"  # approve 时用户已不存在，整个事务回滚，请求仍是 pending


@dataclass(frozen=True, slots=True)
class TopupDecision:
    """`decide_topup_request` 的结果。"""

    outcome: DecisionOutcome
    request: TopupRequest | None = None
    credit: balance.BalanceResult | None = None
    blocked_until: datetime | None = None


def topup_op_key(request_id: int) -> str:
    return balance.make_op_key("topup", request_id)


async def create_topup_request(user_id: int, coins: int, price_cents: int) -> int:
    """记录一条 pending 的充值请求并返回它的 id；管理员按钮只携带这个 id。"""
    async with sql.transaction() as connection:
        return await charge_repository.insert_topup_request(
            connection, user_id, coins, price_cents
        )


async def get_topup_request(request_id: int) -> TopupRequest | None:
    return await charge_repository.get_topup_request(request_id)


async def discard_pending_topup_request(request_id: int) -> None:
    """请求没能送达管理员时撤销它；已经被处理过的请求不受影响。"""
    async with sql.transaction() as connection:
        await charge_repository.delete_pending_topup_request(connection, request_id)


async def decide_topup_request(
    request_id: int,
    action: TopupAction,
    decided_by: int,
    *,
    now: datetime | None = None,
) -> TopupDecision:
    """管理员对充值请求做出决定：pending 只能转换一次。

    先占住转换，影响行数为 1 才继续（approve 以 `topup:<id>` 入账付费金币，block 写入禁用截止时间），
    整个过程一个事务：入账失败时状态保持 pending。两个并发的 approve 只有一个能占住转换。
    """
    action = TopupAction(action)
    new_status = TOPUP_ACTION_STATUS[action]
    now = now or datetime.now()
    request = await charge_repository.get_topup_request(request_id)
    if request is None:
        return TopupDecision(DecisionOutcome.NOT_FOUND)

    try:
        async with sql.transaction() as connection:
            claimed = await charge_repository.claim_pending_topup_request(
                connection, request_id, new_status, now, decided_by
            )
            if not claimed:
                # 锁定读取到的是最新状态；事务里更早的普通读可能停留在旧快照。
                current = await charge_repository.lock_topup_status(connection, request_id)
                return TopupDecision(
                    DecisionOutcome.ALREADY_DECIDED,
                    replace(request, status=current or "unknown"),
                )

            credit = None
            blocked_until = None
            if action is TopupAction.APPROVE:
                credit = await balance.credit(
                    connection,
                    request.user_id,
                    request.coins,
                    op_key=topup_op_key(request.id),
                    reason="topup",
                    kind=balance.CoinKind.PAID,
                )
            elif action is TopupAction.BLOCK:
                blocked_until = now + timedelta(days=1)
                await charge_repository.set_recharge_blocked_until(
                    connection, request.user_id, blocked_until
                )
    except balance.UserNotFound:
        return TopupDecision(DecisionOutcome.USER_MISSING, request)

    return TopupDecision(
        DecisionOutcome.APPLIED,
        replace(request, status=new_status),
        credit,
        blocked_until,
    )


async def get_recharge_blocked_until(user_id: int) -> datetime | None:
    return await charge_repository.get_recharge_blocked_until(user_id)


# ---------------------------------------------------------------------------
# 生成卡密
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GeneratedCodes:
    codes: list[str]
    duplicate_count: int  # 重试用尽仍然重复、没能生成的个数


async def generate_codes(count: int, amount: int) -> GeneratedCodes:
    """生成 `count` 张面值 `amount` 的卡密，整批在一个事务里写入。"""
    codes: list[str] = []
    duplicate_count = 0
    max_retries = 3  # 最大重试次数

    async with sql.transaction() as connection:
        for _ in range(count):
            retry_count = 0
            while retry_count < max_retries:
                unique_code = str(uuid.uuid4())
                if not await charge_repository.code_exists(connection, unique_code):
                    await charge_repository.insert_code(connection, unique_code, amount)
                    codes.append(unique_code)
                    break
                retry_count += 1

            if retry_count >= max_retries:
                duplicate_count += 1
                logger.warning(f"生成唯一卡密失败，重试次数达到上限: {max_retries}")

    return GeneratedCodes(codes, duplicate_count)
