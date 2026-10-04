"""质押奖励池。

奖池的每一次变动都在 `stake_pool_ledger` 里留一行，`op_key` 唯一，所以奖池贡献/扣减同样幂等。
奖池是 DECIMAL(20,2) 且不属于任何用户，因此与 `coin_ledger` 分开记账；op_key 的命名与
事务规则沿用余额服务（docs/balance-service.md）。

典型用法是「某次消费的 20% 进奖池」：消费的 op_key 是 `X`，奖池贡献用 `pool_op_key(X)`，
`credit_share_of_spend` 已经封装好。同一次消费的贡献因此只会入账一次，
和消费放在同一个事务里（先 `balance.debit` 再贡献）或在交付成功后单独记账都可以。

并发：先锁奖池行（`FOR UPDATE`）再查 op_key，所有奖池变动因此串行。事务里同时涉及用户余额时，
加锁顺序固定为「先 user 行，后奖池行」。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from . import balance, sql

POOL_ROW_ID = 1
POOL_RATE = Decimal("0.2")
POOL_QUANT = Decimal("0.01")
POOL_OP_KEY_PREFIX = "pool:"


class PoolKind(StrEnum):
    CREDIT = "credit"
    DEBIT = "debit"


@dataclass(frozen=True, slots=True)
class PoolResult:
    """一次奖池操作的结果；`applied` 为 False 表示同一个 op_key 的重放，未改动任何数据。

    `delta` 带符号（贡献为正，扣减为负），`balance` 是该操作生效后的奖池余额。
    """

    op_key: str
    kind: PoolKind
    applied: bool
    delta: Decimal
    balance: Decimal
    reason: str
    ref: str | None = None


class PoolInsufficient(balance.BalanceError):
    """扣减奖池时余额不足。抛出时没有改动任何数据。"""

    def __init__(self, requested: Decimal, available: Decimal) -> None:
        super().__init__(f"奖池余额不足：需要 {requested}，当前 {available}")
        self.requested = requested
        self.available = available


def _normalize_amount(amount: Any) -> Decimal:
    if amount is None:
        return Decimal("0")
    value = amount if isinstance(amount, Decimal) else Decimal(str(amount))
    if value <= 0:
        return Decimal("0")
    return value.quantize(POOL_QUANT, rounding=ROUND_DOWN)


def calculate_pool_add(cost: int) -> Decimal:
    if cost <= 0:
        return Decimal("0")
    return _normalize_amount(Decimal(cost) * POOL_RATE)


def pool_op_key(spend_op_key: str) -> str:
    """某次消费对应的奖池贡献 op_key。"""
    return _check_pool_op_key(POOL_OP_KEY_PREFIX + balance.check_op_key(spend_op_key))


def _check_pool_op_key(op_key: str) -> str:
    return balance.check_op_key(op_key, max_length=balance.DERIVED_OP_KEY_MAX_LENGTH)


def _check_pool_amount(amount: Decimal | int | str) -> Decimal:
    """奖池金额必须是正数且精确到分；不会静默截断，避免记账与调用方的预期不一致。"""
    try:
        value = amount if isinstance(amount, Decimal) else Decimal(str(amount))
    except ArithmeticError as exc:
        raise balance.InvalidBalanceRequest(f"奖池金额无效: {amount!r}") from exc
    if not value.is_finite() or value <= 0 or value != value.quantize(POOL_QUANT):
        raise balance.InvalidBalanceRequest(f"奖池金额必须是大于 0、精确到 0.01 的数: {amount!r}")
    return value


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------


async def _ensure_pool_row(connection: AsyncConnection | None = None) -> None:
    await sql.execute(
        "INSERT INTO stake_reward_pool (id, balance) VALUES (%s, 0) "
        "ON DUPLICATE KEY UPDATE balance = balance",
        (POOL_ROW_ID,),
        connection=connection,
    )


async def get_pool_balance(
    *,
    connection: AsyncConnection | None = None,
    for_update: bool = False,
) -> Decimal:
    await _ensure_pool_row(connection)
    query = "SELECT balance FROM stake_reward_pool WHERE id = %s"
    if for_update:
        query += " FOR UPDATE"
    row = await sql.fetch_one(query, (POOL_ROW_ID,), connection=connection)
    if not row or row[0] is None:
        return Decimal("0")
    return Decimal(str(row[0]))


_LEDGER_COLUMNS = "op_key, kind, delta, balance_after, reason, ref"


def _entry_from_row(row: Any, *, applied: bool) -> PoolResult:
    return PoolResult(
        op_key=str(row[0]),
        kind=PoolKind(row[1]),
        applied=applied,
        delta=Decimal(str(row[2])),
        balance=Decimal(str(row[3])),
        reason=str(row[4]),
        ref=None if row[5] is None else str(row[5]),
    )


async def _read_entry(
    connection: AsyncConnection,
    op_key: str,
    *,
    for_update: bool,
) -> PoolResult | None:
    suffix = " FOR UPDATE" if for_update else ""
    result = await connection.exec_driver_sql(
        f"SELECT {_LEDGER_COLUMNS} FROM stake_pool_ledger WHERE op_key = %s{suffix}",
        (op_key,),
    )
    row = result.first()
    return None if row is None else _entry_from_row(row, applied=False)


async def get_pool_operation(
    op_key: str,
    *,
    connection: AsyncConnection | None = None,
) -> PoolResult | None:
    """按 op_key 查询已记录的奖池操作；没有记录返回 None。"""
    if connection is not None:
        return await _read_entry(connection, op_key, for_update=False)
    async with sql.connect() as own_connection:
        return await _read_entry(own_connection, op_key, for_update=False)


# ---------------------------------------------------------------------------
# 写入（在调用方的事务里执行）
# ---------------------------------------------------------------------------


def _same_request(existing: PoolResult, *, kind: PoolKind, delta: Decimal) -> None:
    if existing.kind != kind:
        raise balance.OperationConflict(
            existing.op_key, f"类型不同（已有 {existing.kind}，请求 {kind}）"
        )
    if existing.delta != delta:
        raise balance.OperationConflict(
            existing.op_key, f"金额不同（已有 {existing.delta}，请求 {delta}）"
        )


async def _apply(
    connection: AsyncConnection,
    *,
    kind: PoolKind,
    amount: Decimal,
    op_key: str,
    reason: str,
    ref: str | None,
    allow_negative: bool,
) -> PoolResult:
    delta = amount if kind is PoolKind.CREDIT else -amount
    # 先锁奖池行，所有奖池变动因此串行；之后的锁定读不会与其他事务争抢间隙。
    current = await get_pool_balance(connection=connection, for_update=True)

    existing = await _read_entry(connection, op_key, for_update=True)
    if existing is not None:
        _same_request(existing, kind=kind, delta=delta)
        return existing

    if kind is PoolKind.DEBIT and not allow_negative and current < amount:
        raise PoolInsufficient(amount, current)

    new_balance = current + delta
    try:
        await connection.exec_driver_sql(
            "INSERT INTO stake_pool_ledger (op_key, kind, delta, balance_after, reason, ref) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (op_key, kind.value, delta, new_balance, reason, ref),
        )
    except IntegrityError as exc:
        # 奖池行锁已经把写入串行化，走到这里说明有人绕过了服务直接写表。
        if not sql.is_duplicate_key_error(exc):
            raise
        existing = await _read_entry(connection, op_key, for_update=True)
        if existing is None:
            raise
        _same_request(existing, kind=kind, delta=delta)
        return existing

    await connection.exec_driver_sql(
        "UPDATE stake_reward_pool SET balance = %s WHERE id = %s",
        (new_balance, POOL_ROW_ID),
    )
    return PoolResult(
        op_key=op_key,
        kind=kind,
        applied=True,
        delta=delta,
        balance=new_balance,
        reason=reason,
        ref=ref,
    )


async def credit_pool(
    connection: AsyncConnection,
    amount: Decimal | int | str,
    *,
    op_key: str,
    reason: str,
    ref: str | None = None,
) -> PoolResult:
    """向奖池贡献 `amount`（正数，精确到 0.01）。同一个 op_key 重放返回原结果。

    raises: OperationConflict、InvalidBalanceRequest
    """
    return await _apply(
        connection,
        kind=PoolKind.CREDIT,
        amount=_check_pool_amount(amount),
        op_key=_check_pool_op_key(op_key),
        reason=balance.check_reason(reason),
        ref=balance.check_ref(ref),
        allow_negative=False,
    )


async def debit_pool(
    connection: AsyncConnection,
    amount: Decimal | int | str,
    *,
    op_key: str,
    reason: str,
    ref: str | None = None,
) -> PoolResult:
    """从奖池扣减 `amount`；余额不足抛 PoolInsufficient，未改动任何数据。

    raises: PoolInsufficient、OperationConflict、InvalidBalanceRequest
    """
    return await _apply(
        connection,
        kind=PoolKind.DEBIT,
        amount=_check_pool_amount(amount),
        op_key=_check_pool_op_key(op_key),
        reason=balance.check_reason(reason),
        ref=balance.check_ref(ref),
        allow_negative=False,
    )


async def credit_share_of_spend(
    connection: AsyncConnection,
    spent_coins: int,
    *,
    spend_op_key: str,
    reason: str = "spend_share",
) -> PoolResult | None:
    """把一次消费 `spent_coins` 的 `POOL_RATE` 贡献进奖池，op_key 由消费的 op_key 派生。

    贡献不足 0.01 时什么都不做并返回 None。
    """
    pool_add = calculate_pool_add(spent_coins)
    if pool_add <= 0:
        return None
    return await credit_pool(
        connection,
        pool_add,
        op_key=pool_op_key(spend_op_key),
        reason=reason,
        ref=spend_op_key,
    )


# ---------------------------------------------------------------------------
# 自己开事务的便捷包装
# ---------------------------------------------------------------------------


async def credit_pool_standalone(
    amount: Decimal | int | str,
    *,
    op_key: str,
    reason: str,
    ref: str | None = None,
) -> PoolResult:
    return await balance.run_in_transaction(
        lambda connection: credit_pool(connection, amount, op_key=op_key, reason=reason, ref=ref)
    )


async def debit_pool_standalone(
    amount: Decimal | int | str,
    *,
    op_key: str,
    reason: str,
    ref: str | None = None,
) -> PoolResult:
    return await balance.run_in_transaction(
        lambda connection: debit_pool(connection, amount, op_key=op_key, reason=reason, ref=ref)
    )


async def credit_share_of_spend_standalone(
    spent_coins: int,
    *,
    spend_op_key: str,
    reason: str = "spend_share",
) -> PoolResult | None:
    return await balance.run_in_transaction(
        lambda connection: credit_share_of_spend(
            connection, spent_coins, spend_op_key=spend_op_key, reason=reason
        )
    )


# ---------------------------------------------------------------------------
# 旧接口（待移除）
# ---------------------------------------------------------------------------


async def add_to_pool(amount: Any, *, connection: AsyncConnection | None = None) -> Decimal:
    """待移除：旧调用方（stake_coin 等）尚未迁移到 `credit_pool`。

    委托给奖池账本，每次调用生成一次性 op_key（reason 为 `legacy:add_to_pool`），
    保证仍然留下账本记录，但**不提供**重放保护。
    """
    value = _normalize_amount(amount)
    if value <= 0:
        return Decimal("0")
    op_key = balance.new_op_key("legacy:pool_add")
    if connection is None:
        await credit_pool_standalone(value, op_key=op_key, reason="legacy:add_to_pool")
    else:
        await credit_pool(connection, value, op_key=op_key, reason="legacy:add_to_pool")
    return value


async def subtract_from_pool(amount: Any, *, connection: AsyncConnection | None = None) -> Decimal:
    """待移除：旧调用方尚未迁移到 `debit_pool`。

    与旧行为一致，不检查余额（可以扣成负数）；同样写一条一次性 op_key 的账本记录。
    """
    value = _normalize_amount(amount)
    if value <= 0:
        return Decimal("0")
    op_key = balance.new_op_key("legacy:pool_sub")

    async def work(conn: AsyncConnection) -> PoolResult:
        return await _apply(
            conn,
            kind=PoolKind.DEBIT,
            amount=value,
            op_key=op_key,
            reason="legacy:subtract_from_pool",
            ref=None,
            allow_negative=True,
        )

    if connection is None:
        await balance.run_in_transaction(work)
    else:
        await work(connection)
    return value
