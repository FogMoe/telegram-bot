"""余额服务：金币变动的唯一入口。

所有金币变动都落在 `coin_ledger` 里，一次变动一行，`op_key` 唯一。完整契约（命名规范、
事务所有权、失败与退款规则、对账方法）见 docs/balance-service.md，这里只写实现要点。

API
---
- `credit(connection, user_id, amount, *, op_key, reason, kind=CoinKind.FREE, ref=None)`
- `debit(connection, user_id, amount, *, op_key, reason, ref=None)`：先扣免费再扣付费
- `refund(connection, original_op_key, *, reason="refund")`：把一次成功的 debit 原路退回
- `get_operation(op_key)`、`lock_user(connection, user_id)`、`lock_users(connection, user_ids)`（多个用户按 id 升序加锁）
- `credit_standalone` / `debit_standalone` / `refund_standalone`：自己开事务的便捷包装

核心操作都在调用方传入的 `connection`（事务）里执行，不提交也不回滚；调用方可以在同一个事务里
写自己的业务状态。余额不足、用户不存在、op_key 冲突都以异常报告，且都发生在任何写入之前，
调用方不处理就会让事务回滚。

并发
----
先 `SELECT ... FOR UPDATE` 锁住 user 行，同一用户的所有变动因此串行。op_key 的判重以唯一约束
为准：先尝试 INSERT，撞上唯一键再用锁定读取回已有记录。不先对尚不存在的 op_key 做锁定读，
是因为 REPEATABLE READ 下它会加间隙锁，不同用户的两个事务只要 op_key 落在同一个间隙，
各自的 INSERT 就会互相等待而死锁。已存在记录的锁定读只锁记录本身。
REPEATABLE READ 下普通一致性读可能看不到已提交的并发写，所以服务内部凡是要读「最新」的
地方一律用锁定读；调用方也不要用事务里更早的普通 SELECT 判断余额。
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from . import config, sql

USER_PLAN_FREE = "free"
USER_PLAN_PAID = "paid"
USER_PLAN_ADMIN = "admin"


def resolve_user_plan(user_id: int, coins_paid: int) -> str:
    """user_plan 完全由 user_id 与付费余额推出：管理员是 admin，有付费余额是 paid。"""
    if user_id == config.ADMIN_USER_ID:
        return USER_PLAN_ADMIN
    return USER_PLAN_PAID if coins_paid > 0 else USER_PLAN_FREE


# ---------------------------------------------------------------------------
# op_key
# ---------------------------------------------------------------------------

OP_KEY_MAX_LENGTH = 128
REF_MAX_LENGTH = 160
DERIVED_OP_KEY_MAX_LENGTH = 160  # 列宽；refund:/pool: 前缀加上原 op_key 仍然放得下
REASON_MAX_LENGTH = 64
MAX_AMOUNT = 2_147_483_647  # user.coins 是 INT
REFUND_PREFIX = "refund:"

# 可打印 ASCII，不含空白；与 ledger 里 ascii_bin 的列定义对应。
_ASCII_TOKEN = re.compile(r"[\x21-\x7e]+")


def make_op_key(*parts: object) -> str:
    """用 `:` 连接各部分得到 op_key，并校验长度与字符集。

    各部分必须是持久身份（用户、日期、请求 id、消息 id 等），例如
    `make_op_key("checkin", user_id, today)` -> `checkin:123:2026-10-05`。
    """
    return check_op_key(":".join(str(part) for part in parts))


def new_op_key(prefix: str) -> str:
    """一次性 op_key。没有持久身份可用时的退路：每次调用都不同，因此**不提供**重放保护。"""
    return check_op_key(f"{prefix}:{uuid.uuid4().hex}")


def refund_op_key(original_op_key: str) -> str:
    """`refund(original_op_key)` 记账用的 op_key，由原 op_key 派生。"""
    return REFUND_PREFIX + original_op_key


def check_op_key(op_key: str, *, max_length: int = OP_KEY_MAX_LENGTH) -> str:
    """校验调用方给出的 op_key。`max_length` 只给派生 op_key（如奖池贡献）放宽，列宽是 160。"""
    if not isinstance(op_key, str) or not 0 < len(op_key) <= max_length:
        raise InvalidBalanceRequest(f"op_key 长度必须在 1..{max_length} 之间")
    if not _ASCII_TOKEN.fullmatch(op_key):
        raise InvalidBalanceRequest("op_key 只能包含可打印的 ASCII 字符，不能有空白")
    if op_key.startswith(REFUND_PREFIX):
        raise InvalidBalanceRequest(f"op_key 不能以保留前缀 {REFUND_PREFIX!r} 开头")
    return op_key


def check_ref(ref: str | None) -> str | None:
    if ref is None:
        return None
    if not isinstance(ref, str) or not 0 < len(ref) <= REF_MAX_LENGTH:
        raise InvalidBalanceRequest(f"ref 长度必须在 1..{REF_MAX_LENGTH} 之间")
    if not _ASCII_TOKEN.fullmatch(ref):
        raise InvalidBalanceRequest("ref 只能包含可打印的 ASCII 字符，不能有空白")
    return ref


def check_reason(reason: str) -> str:
    if not isinstance(reason, str) or not 0 < len(reason) <= REASON_MAX_LENGTH:
        raise InvalidBalanceRequest(f"reason 长度必须在 1..{REASON_MAX_LENGTH} 之间")
    return reason


def check_amount(amount: int) -> int:
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise InvalidBalanceRequest(f"金额必须是整数: {amount!r}")
    if not 0 < amount <= MAX_AMOUNT:
        raise InvalidBalanceRequest(f"金额必须在 1..{MAX_AMOUNT} 之间: {amount}")
    return amount


# ---------------------------------------------------------------------------
# 类型与异常
# ---------------------------------------------------------------------------


class CoinKind(StrEnum):
    FREE = "free"
    PAID = "paid"


class LedgerKind(StrEnum):
    CREDIT = "credit"
    DEBIT = "debit"
    REFUND = "refund"


@dataclass(frozen=True, slots=True)
class UserBalances:
    free: int
    paid: int

    @property
    def total(self) -> int:
        return self.free + self.paid


@dataclass(frozen=True, slots=True)
class BalanceResult:
    """一次余额操作的结果。

    `applied` 为 False 表示这是同一个 op_key 的重放：本次调用没有改动任何数据，
    其余字段是这次操作最初生效时记录的值（`balance_*` 是当时变动后的余额，不是当前余额）。
    `delta_*` 带符号：入账为正，扣款为负，退款为正。
    """

    op_key: str
    user_id: int
    kind: LedgerKind
    applied: bool
    delta_free: int
    delta_paid: int
    balance_free: int
    balance_paid: int
    reason: str
    ref: str | None = None

    @property
    def amount(self) -> int:
        """变动的金币总数（绝对值）。"""
        return abs(self.delta_free + self.delta_paid)

    @property
    def balance_total(self) -> int:
        return self.balance_free + self.balance_paid

    @property
    def user_plan(self) -> str:
        return resolve_user_plan(self.user_id, self.balance_paid)


class BalanceError(Exception):
    """余额服务所有异常的基类。"""


class InvalidBalanceRequest(BalanceError, ValueError):
    """金额、op_key、reason 等参数不合法。"""


class UserNotFound(BalanceError):
    def __init__(self, user_id: int) -> None:
        super().__init__(f"用户不存在: {user_id}")
        self.user_id = user_id


class InsufficientBalance(BalanceError):
    """扣款时余额不足。抛出时没有改动任何数据。"""

    def __init__(self, user_id: int, requested: int, balances: UserBalances) -> None:
        super().__init__(
            f"用户 {user_id} 余额不足：需要 {requested}，"
            f"免费 {balances.free} + 付费 {balances.paid}"
        )
        self.user_id = user_id
        self.requested = requested
        self.balance_free = balances.free
        self.balance_paid = balances.paid

    @property
    def balance_total(self) -> int:
        return self.balance_free + self.balance_paid


class OperationConflict(BalanceError):
    """同一个 op_key 已经用不同的参数（用户、金额、类型）记录过。"""

    def __init__(self, op_key: str, detail: str) -> None:
        super().__init__(f"op_key {op_key!r} 与已有记录冲突：{detail}")
        self.op_key = op_key
        self.detail = detail


class RefundRejected(BalanceError):
    """退款对象不存在，或不是一次成功的 debit。"""

    def __init__(self, original_op_key: str, detail: str) -> None:
        super().__init__(f"无法退款 {original_op_key!r}：{detail}")
        self.original_op_key = original_op_key
        self.detail = detail


# ---------------------------------------------------------------------------
# 核心操作（在调用方的事务里执行）
# ---------------------------------------------------------------------------

_LEDGER_COLUMNS = (
    "op_key, user_id, kind, delta_free, delta_paid, balance_free, balance_paid, reason, ref"
)


def split_debit(balances: UserBalances, amount: int) -> tuple[int, int]:
    """扣款的拆分：先扣免费再扣付费，返回带符号的 (delta_free, delta_paid)。

    不检查余额是否足够：余额不足时付费部分会超出 `balances.paid`，由调用方先判断。
    """
    take_free = min(balances.free, amount)
    return -take_free, -(amount - take_free)


async def lock_user(connection: AsyncConnection, user_id: int) -> UserBalances:
    """`SELECT ... FOR UPDATE` 锁住 user 行并返回当前余额；用户不存在抛 UserNotFound。

    持有这把锁期间，同一用户的其他余额变动都会等待。需要在变动之前先做资格判断的业务
    （签到、抽奖）可以先调用它，再在锁内读取并判断自己的状态。
    """
    result = await connection.exec_driver_sql(
        "SELECT coins, coins_paid FROM user WHERE id = %s FOR UPDATE",
        (user_id,),
    )
    row = result.first()
    if row is None:
        raise UserNotFound(user_id)
    return UserBalances(free=int(row[0] or 0), paid=int(row[1] or 0))


async def lock_users(
    connection: AsyncConnection, user_ids: Iterable[int]
) -> dict[int, UserBalances]:
    """按 user id 升序依次锁住多个 user 行，返回每个用户当前的余额。

    一个事务要同时变动多个用户（转账、邀请奖励）时必须走这里：所有路径都按同一顺序加锁，
    A 转给 B 与 B 转给 A 同时发生也不会互相等待。任何一个用户不存在抛 UserNotFound
    （带上缺失的 user id）。
    """
    balances: dict[int, UserBalances] = {}
    for user_id in sorted(set(user_ids)):
        balances[user_id] = await lock_user(connection, user_id)
    return balances


def _entry_from_row(row: Any, *, applied: bool) -> BalanceResult:
    return BalanceResult(
        op_key=str(row[0]),
        user_id=int(row[1]),
        kind=LedgerKind(row[2]),
        applied=applied,
        delta_free=int(row[3]),
        delta_paid=int(row[4]),
        balance_free=int(row[5]),
        balance_paid=int(row[6]),
        reason=str(row[7]),
        ref=None if row[8] is None else str(row[8]),
    )


async def _read_entry(
    connection: AsyncConnection,
    op_key: str,
    *,
    for_update: bool,
) -> BalanceResult | None:
    suffix = " FOR UPDATE" if for_update else ""
    result = await connection.exec_driver_sql(
        f"SELECT {_LEDGER_COLUMNS} FROM coin_ledger WHERE op_key = %s{suffix}",
        (op_key,),
    )
    row = result.first()
    return None if row is None else _entry_from_row(row, applied=False)


def _conflict_detail(
    existing: BalanceResult,
    *,
    kind: LedgerKind,
    user_id: int,
    delta_free: int,
    delta_paid: int,
    ref: str | None,
) -> str | None:
    """已有记录与这次请求的参数不一致时返回原因，一致返回 None。

    reason 只是说明文字，不属于操作身份。扣款的免费/付费拆分取决于当时的余额，
    重放时余额可能已经变了，所以扣款只比较总额。
    """
    if existing.kind != kind:
        return f"类型不同（已有 {existing.kind}，请求 {kind}）"
    if existing.user_id != user_id:
        return f"用户不同（已有 {existing.user_id}，请求 {user_id}）"
    if kind is LedgerKind.CREDIT:
        if (existing.delta_free, existing.delta_paid) != (delta_free, delta_paid):
            return (
                f"金额不同（已有 免费{existing.delta_free:+d} 付费{existing.delta_paid:+d}，"
                f"请求 免费{delta_free:+d} 付费{delta_paid:+d}）"
            )
    elif kind is LedgerKind.DEBIT:
        if existing.amount != abs(delta_free + delta_paid):
            return f"金额不同（已有 {existing.amount}，请求 {abs(delta_free + delta_paid)}）"
    elif existing.ref != ref:
        return f"退款对象不同（已有 {existing.ref}，请求 {ref}）"
    return None


def _replay(
    existing: BalanceResult,
    *,
    kind: LedgerKind,
    user_id: int,
    delta_free: int,
    delta_paid: int,
    ref: str | None,
) -> BalanceResult:
    detail = _conflict_detail(
        existing,
        kind=kind,
        user_id=user_id,
        delta_free=delta_free,
        delta_paid=delta_paid,
        ref=ref,
    )
    if detail is not None:
        raise OperationConflict(existing.op_key, detail)
    return existing


async def _apply(
    connection: AsyncConnection,
    *,
    kind: LedgerKind,
    user_id: int,
    op_key: str,
    delta_free: int,
    delta_paid: int,
    balances: UserBalances,
    reason: str,
    ref: str | None,
) -> BalanceResult:
    """写账本行与 user 行。调用方已经持有 user 行锁，且已确认余额足够。

    先 INSERT 账本再 UPDATE user：撞上唯一键时语句被回滚，user 行还没被改动。
    """
    new_free = balances.free + delta_free
    new_paid = balances.paid + delta_paid
    try:
        await connection.exec_driver_sql(
            "INSERT INTO coin_ledger (op_key, user_id, kind, delta_free, delta_paid, "
            "balance_free, balance_paid, reason, ref) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                op_key,
                user_id,
                kind.value,
                delta_free,
                delta_paid,
                new_free,
                new_paid,
                reason,
                ref,
            ),
        )
    except IntegrityError as exc:
        if not sql.is_duplicate_key_error(exc):
            raise
        existing = await _read_entry(connection, op_key, for_update=True)
        if existing is None:
            raise
        return _replay(
            existing,
            kind=kind,
            user_id=user_id,
            delta_free=delta_free,
            delta_paid=delta_paid,
            ref=ref,
        )

    await connection.exec_driver_sql(
        "UPDATE user SET coins = %s, coins_paid = %s, user_plan = %s WHERE id = %s",
        (new_free, new_paid, resolve_user_plan(user_id, new_paid), user_id),
    )
    return BalanceResult(
        op_key=op_key,
        user_id=user_id,
        kind=kind,
        applied=True,
        delta_free=delta_free,
        delta_paid=delta_paid,
        balance_free=new_free,
        balance_paid=new_paid,
        reason=reason,
        ref=ref,
    )


async def credit(
    connection: AsyncConnection,
    user_id: int,
    amount: int,
    *,
    op_key: str,
    reason: str,
    kind: CoinKind = CoinKind.FREE,
    ref: str | None = None,
) -> BalanceResult:
    """入账 `amount` 枚金币。付费入账同时把 user_plan 推导为 paid（管理员保持 admin）。

    raises: UserNotFound、OperationConflict、InvalidBalanceRequest
    """
    amount = check_amount(amount)
    op_key = check_op_key(op_key)
    reason = check_reason(reason)
    ref = check_ref(ref)
    coin_kind = CoinKind(kind)
    delta_free = amount if coin_kind is CoinKind.FREE else 0
    delta_paid = amount if coin_kind is CoinKind.PAID else 0

    balances = await lock_user(connection, user_id)
    return await _apply(
        connection,
        kind=LedgerKind.CREDIT,
        user_id=user_id,
        op_key=op_key,
        delta_free=delta_free,
        delta_paid=delta_paid,
        balances=balances,
        reason=reason,
        ref=ref,
    )


async def debit(
    connection: AsyncConnection,
    user_id: int,
    amount: int,
    *,
    op_key: str,
    reason: str,
    ref: str | None = None,
) -> BalanceResult:
    """扣款 `amount` 枚金币，先扣免费再扣付费。

    raises: InsufficientBalance（余额不足，未改动任何数据）、UserNotFound、
    OperationConflict、InvalidBalanceRequest。已经成功过的 op_key 重放时不检查当前余额，
    直接返回原结果。
    """
    amount = check_amount(amount)
    op_key = check_op_key(op_key)
    reason = check_reason(reason)
    ref = check_ref(ref)

    balances = await lock_user(connection, user_id)
    delta_free, delta_paid = split_debit(balances, amount)

    if balances.total < amount:
        # 余额不足也可能是重放（原操作成功后余额被花掉了）：先看有没有已有记录。
        existing = await _read_entry(connection, op_key, for_update=True)
        if existing is not None:
            return _replay(
                existing,
                kind=LedgerKind.DEBIT,
                user_id=user_id,
                delta_free=delta_free,
                delta_paid=delta_paid,
                ref=ref,
            )
        raise InsufficientBalance(user_id, amount, balances)

    return await _apply(
        connection,
        kind=LedgerKind.DEBIT,
        user_id=user_id,
        op_key=op_key,
        delta_free=delta_free,
        delta_paid=delta_paid,
        balances=balances,
        reason=reason,
        ref=ref,
    )


async def refund(
    connection: AsyncConnection,
    original_op_key: str,
    *,
    reason: str = "refund",
) -> BalanceResult:
    """把一次成功的 debit 按原来的免费/付费拆分原路退回。

    退款的 op_key 由原 op_key 派生（`refund_op_key`），所以可以重复调用，只会退一次；
    重复调用返回 `applied=False`。

    raises: RefundRejected（原操作不存在或不是 debit）、UserNotFound、InvalidBalanceRequest
    """
    original_op_key = check_op_key(original_op_key)
    reason = check_reason(reason)

    # 只为取得用户 id：先用普通读，看不到（本事务的快照早于原操作提交）再用锁定读。
    # 先锁账本行再锁用户行与其他路径的加锁顺序相反，所以优先走不加锁的读取。
    original = await _read_entry(connection, original_op_key, for_update=False)
    if original is None:
        original = await _read_entry(connection, original_op_key, for_update=True)
    if original is None:
        raise RefundRejected(original_op_key, "没有这条操作记录")
    if original.kind is not LedgerKind.DEBIT:
        raise RefundRejected(original_op_key, f"只能退回 debit，这条记录是 {original.kind}")

    balances = await lock_user(connection, original.user_id)
    return await _apply(
        connection,
        kind=LedgerKind.REFUND,
        user_id=original.user_id,
        op_key=refund_op_key(original_op_key),
        delta_free=-original.delta_free,
        delta_paid=-original.delta_paid,
        balances=balances,
        reason=reason,
        ref=original_op_key,
    )


async def get_operation(
    op_key: str,
    *,
    connection: AsyncConnection | None = None,
    for_update: bool = False,
) -> BalanceResult | None:
    """按 op_key 查询已记录的操作（返回值的 `applied` 恒为 False）；没有记录返回 None。

    不传 `connection` 时用一次性连接做普通读。要和调用方事务里的写入保持一致，传入 `connection`。
    """
    if connection is not None:
        return await _read_entry(connection, op_key, for_update=for_update)
    async with sql.connect() as own_connection:
        return await _read_entry(own_connection, op_key, for_update=False)


# ---------------------------------------------------------------------------
# 自己开事务的便捷包装
# ---------------------------------------------------------------------------

_DEADLOCK_ATTEMPTS = 3
_DEADLOCK_BACKOFF_SECONDS = 0.05


async def run_in_transaction[T](work: Callable[[AsyncConnection], Awaitable[T]]) -> T:
    """开一个事务执行 `work(connection)`，死锁时整个事务重跑（MySQL 已回滚整个事务）。

    `work` 必须能安全地重跑：只写事务内的数据，不做事务外的副作用。
    """
    attempt = 1
    while True:
        try:
            async with sql.transaction() as connection:
                return await work(connection)
        except Exception as exc:
            if attempt >= _DEADLOCK_ATTEMPTS or not sql.is_deadlock_error(exc):
                raise
            await asyncio.sleep(_DEADLOCK_BACKOFF_SECONDS * attempt)
            attempt += 1


async def credit_standalone(
    user_id: int,
    amount: int,
    *,
    op_key: str,
    reason: str,
    kind: CoinKind = CoinKind.FREE,
    ref: str | None = None,
) -> BalanceResult:
    """`credit` 的自开事务版本，仅用于没有其他业务状态要一起提交的场合。"""
    return await run_in_transaction(
        lambda connection: credit(
            connection, user_id, amount, op_key=op_key, reason=reason, kind=kind, ref=ref
        )
    )


async def debit_standalone(
    user_id: int,
    amount: int,
    *,
    op_key: str,
    reason: str,
    ref: str | None = None,
) -> BalanceResult:
    """`debit` 的自开事务版本。余额不足时抛 InsufficientBalance。"""
    return await run_in_transaction(
        lambda connection: debit(
            connection, user_id, amount, op_key=op_key, reason=reason, ref=ref
        )
    )


async def refund_standalone(original_op_key: str, *, reason: str = "refund") -> BalanceResult:
    """`refund` 的自开事务版本。"""
    return await run_in_transaction(
        lambda connection: refund(connection, original_op_key, reason=reason)
    )


# ---------------------------------------------------------------------------
# 对账
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DriftReport:
    """账本与实际余额的偏差；三项都为空/False 表示一致。"""

    balance_mismatches: tuple[int, ...]  # user 表余额与账本末行不一致的用户
    broken_chains: tuple[int, ...]  # 账本相邻两行没有首尾相接的用户
    pool_mismatch: bool  # 奖池余额与奖池账本末行不一致

    @property
    def clean(self) -> bool:
        return not (self.balance_mismatches or self.broken_chains or self.pool_mismatch)


# 每个用户账本的末行余额必须等于 user 表。第一行之前的余额（开户奖励等）不在账本里，
# 由第一行的「变动后余额 - 变动量」隐含，所以只比较末行。
_BALANCE_MISMATCH_SQL = """
SELECT u.id FROM user u
JOIN coin_ledger l ON l.id = (SELECT MAX(id) FROM coin_ledger WHERE user_id = u.id)
WHERE u.coins <> l.balance_free OR u.coins_paid <> l.balance_paid
ORDER BY u.id
"""

# 相邻两行首尾相接：本行的「变动前余额」等于上一行的「变动后余额」。
# 不相接说明两行之间有没经过账本的余额变动（直接改 user 表）。
_BROKEN_CHAIN_SQL = """
SELECT DISTINCT cur.user_id FROM coin_ledger cur
JOIN coin_ledger prev
  ON prev.id = (SELECT MAX(id) FROM coin_ledger WHERE user_id = cur.user_id AND id < cur.id)
WHERE cur.balance_free - cur.delta_free <> prev.balance_free
   OR cur.balance_paid - cur.delta_paid <> prev.balance_paid
ORDER BY cur.user_id
"""

_POOL_MISMATCH_SQL = """
SELECT COUNT(*) FROM stake_reward_pool p
JOIN stake_pool_ledger l ON l.id = (SELECT MAX(id) FROM stake_pool_ledger)
WHERE p.id = 1 AND p.balance <> l.balance_after
"""


async def audit_ledger(connection: AsyncConnection | None = None) -> DriftReport:
    """核对账本与实际余额（只读，会扫描整张账本，不要放进请求路径）。"""

    async def work(conn: AsyncConnection) -> DriftReport:
        mismatches = (await conn.exec_driver_sql(_BALANCE_MISMATCH_SQL)).all()
        chains = (await conn.exec_driver_sql(_BROKEN_CHAIN_SQL)).all()
        pool = (await conn.exec_driver_sql(_POOL_MISMATCH_SQL)).scalar_one()
        return DriftReport(
            balance_mismatches=tuple(int(row[0]) for row in mismatches),
            broken_chains=tuple(int(row[0]) for row in chains),
            pool_mismatch=bool(pool),
        )

    if connection is not None:
        return await work(connection)
    async with sql.connect() as own_connection:
        return await work(own_connection)
