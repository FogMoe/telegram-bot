import logging
import random
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncConnection

from . import balance, mysql_connection
# 套餐常量与推导规则的定义在 balance 里，这里原样转出，旧代码的 import 路径不变。
from .balance import USER_PLAN_ADMIN as USER_PLAN_ADMIN
from .balance import USER_PLAN_FREE as USER_PLAN_FREE
from .balance import USER_PLAN_PAID as USER_PLAN_PAID
from .balance import resolve_user_plan as resolve_user_plan

logger = logging.getLogger(__name__)

# 添加用户抽奖锁字典，防止同一用户并发抽奖
lottery_locks = {}

LOTTERY_COOLDOWN = timedelta(hours=24)
LOTTERY_PROBABILITIES = [0.4, 0.1, 0.5]


async def get_user_last_lottery_date(user_id, *, connection=None):
    row = await mysql_connection.fetch_one(
        "SELECT last_lottery_date FROM user_lottery WHERE user_id = %s",
        (user_id,),
        connection=connection,
    )
    return row[0] if row else None


async def update_user_lottery_date(user_id, *, connection=None, when=None):
    await mysql_connection.execute(
        "INSERT INTO user_lottery (user_id, last_lottery_date) VALUES (%s, %s) "
        "ON DUPLICATE KEY UPDATE last_lottery_date = VALUES(last_lottery_date)",
        (user_id, when or datetime.now()),
        connection=connection,
    )


async def get_user_coin_balances(user_id, *, connection=None) -> tuple[int, int]:
    row = await mysql_connection.fetch_one(
        "SELECT coins, coins_paid FROM user WHERE id = %s",
        (user_id,),
        connection=connection,
    )
    if not row:
        return 0, 0
    coins_free = row[0] or 0
    coins_paid = row[1] or 0
    return coins_free, coins_paid


async def get_user_total_coins(user_id, *, connection=None) -> int:
    coins_free, coins_paid = await get_user_coin_balances(
        user_id,
        connection=connection,
    )
    return coins_free + coins_paid


# ---------------------------------------------------------------------------
# 旧的金币接口（待移除）
#
# D2/D3 尚未迁移的调用方还在用下面五个函数。它们现在委托给 core.balance：每次调用生成
# 一次性 op_key、reason 带 `legacy:` 前缀，所以仍然写 coin_ledger，但**没有**重放保护，
# 也没有持久身份可供对账。迁移完所有调用方后整组删除，新代码一律直接用 core.balance。
# ---------------------------------------------------------------------------


async def _legacy_credit(
    user_id, coins, *, kind: balance.CoinKind, reason: str, connection: AsyncConnection | None
) -> int:
    coins = int(coins)
    if coins <= 0:
        return 0
    op_key = balance.new_op_key(reason)
    try:
        if connection is None:
            await balance.credit_standalone(
                user_id, coins, op_key=op_key, reason=reason, kind=kind
            )
        else:
            await balance.credit(
                connection, user_id, coins, op_key=op_key, reason=reason, kind=kind
            )
    except balance.UserNotFound:
        logger.warning("%s: 用户不存在，没有入账 user_id=%s", reason, user_id)
        return 0
    return coins


async def add_free_coins(user_id, coins, *, connection=None) -> int:
    """待移除：改用 `balance.credit`。"""
    return await _legacy_credit(
        user_id,
        coins,
        kind=balance.CoinKind.FREE,
        reason="legacy:add_free_coins",
        connection=connection,
    )


async def add_paid_coins(user_id, coins, *, connection=None) -> int:
    """待移除：改用 `balance.credit(..., kind=CoinKind.PAID)`。"""
    return await _legacy_credit(
        user_id,
        coins,
        kind=balance.CoinKind.PAID,
        reason="legacy:add_paid_coins",
        connection=connection,
    )


async def spend_user_coins(user_id, amount, *, connection=None) -> bool:
    """待移除：改用 `balance.debit`，它用异常区分余额不足与用户不存在。

    返回 False 表示余额不足或用户不存在；调用方必须检查返回值。
    """
    amount = int(amount)
    if amount <= 0:
        return True
    reason = "legacy:spend_user_coins"
    op_key = balance.new_op_key(reason)
    try:
        if connection is None:
            await balance.debit_standalone(user_id, amount, op_key=op_key, reason=reason)
        else:
            await balance.debit(connection, user_id, amount, op_key=op_key, reason=reason)
    except (balance.InsufficientBalance, balance.UserNotFound):
        return False
    return True


async def update_user_coins(user_id, coins, *, connection=None):
    """待移除：正数入账免费金币，负数扣款；改用 `balance.credit` / `balance.debit`。"""
    coins = int(coins)
    if coins >= 0:
        return await add_free_coins(user_id, coins, connection=connection)
    return await spend_user_coins(user_id, -coins, connection=connection)


async def user_exists(user_id):
    row = await mysql_connection.fetch_one(
        "SELECT id FROM user WHERE id = %s",
        (user_id,),
    )
    return row is not None


async def async_user_exists(user_id):
    return await user_exists(user_id)


def draw_lottery_coins(rng: random.Random | None = None) -> int:
    """抽一次奖励：40% 为 1-4，10% 为 11-20，50% 为 5-10，档内均匀。"""
    rng = rng or random.Random()
    coins_distribution = [
        rng.choice(range(1, 5)),
        rng.choice(range(11, 21)),
        rng.choice(range(5, 11)),
    ]
    return rng.choices(coins_distribution, LOTTERY_PROBABILITIES)[0]


def lottery_op_key(user_id: int, last_lottery_date: datetime | None) -> str:
    """一次抽奖资格对应的 op_key：由用户与「上一次抽奖时间」决定。

    资格窗口只会随着时间戳写入而前进，所以同一个窗口里的重试、并发都得到同一个 op_key，
    奖励只能入账一次；窗口前进之后才会出现新的 op_key。从没抽过奖记为 `never`。
    """
    stamp = "never" if last_lottery_date is None else last_lottery_date.strftime("%Y%m%dT%H%M%S")
    return balance.make_op_key("lottery", user_id, stamp)


async def lottery(user_id):
    """抽奖：资格判断、奖励入账、时间戳写入在同一个事务里，要么全部生效要么都不生效。"""
    async with mysql_connection.transaction() as connection:
        try:
            # 先锁用户行，同一用户的抽奖、签到等余额变动都在这里串行。
            await balance.lock_user(connection, user_id)
        except balance.UserNotFound:
            return (
                "请先使用 /me 命令获取个人信息。\n"
                "Please register first using the /me command."
            )

        # 用户行已经锁住，这个事务的第一次一致性读发生在拿到锁之后，能看到上一个持锁者
        # 提交的时间戳。不对 user_lottery 做 FOR UPDATE：行可能还不存在，间隙锁会让
        # 两个首次抽奖的用户互相死锁。
        last_lottery_date = await get_user_last_lottery_date(user_id, connection=connection)
        if last_lottery_date and datetime.now() - last_lottery_date < LOTTERY_COOLDOWN:
            return (
                "每24小时您只能参加一次抽奖喵。下次再来吧！\n"
                "You can only participate in the lottery once every 24 hours. Meow! Come back later!"
            )

        coins = draw_lottery_coins()
        await balance.credit(
            connection,
            user_id,
            coins,
            op_key=lottery_op_key(user_id, last_lottery_date),
            reason="lottery",
        )
        await update_user_lottery_date(user_id, connection=connection)

    return (
        f"恭喜！您赢得了 {coins} 枚硬币喵。\n"
        f"Congratulations! You have won {coins} coins. Meow!"
    )


async def async_lottery(user_id):
    if user_id in lottery_locks:
        return (
            "抽奖操作过于频繁，请等待上一次操作完成。\n"
            "You're drawing too fast, please wait for the previous lottery to complete."
        )

    try:
        lottery_locks[user_id] = True
        return await lottery(user_id)
    finally:
        lottery_locks.pop(user_id, None)


async def get_user_personal_info(user_id: int) -> str:
    row = await mysql_connection.fetch_one(
        "SELECT info FROM user WHERE id = %s",
        (user_id,),
    )
    if not row or row[0] is None or row[0] == "":
        return ""
    return str(row[0])


async def get_user_coins(user_id: int) -> int:
    return await get_user_total_coins(user_id)


async def async_get_user_coins(user_id: int) -> int:
    return await get_user_coins(user_id)


async def get_user_affection(user_id: int) -> int:
    row = await mysql_connection.fetch_one(
        "SELECT affection FROM ai_user_affection WHERE user_id = %s",
        (user_id,),
    )
    return row[0] if row else 0


def get_user_affection_sync(user_id: int) -> int:
    return mysql_connection.run_sync(get_user_affection(user_id))


async def update_user_affection(user_id: int, delta: int) -> int:
    delta = int(delta)
    if delta > 10:
        delta = 10
    elif delta < -10:
        delta = -10

    async with mysql_connection.transaction() as connection:
        row = await mysql_connection.fetch_one(
            "SELECT affection FROM ai_user_affection WHERE user_id = %s FOR UPDATE",
            (user_id,),
            connection=connection,
        )
        current = row[0] if row else 0
        updated = max(-100, min(100, current + delta))

        if row:
            await connection.exec_driver_sql(
                "UPDATE ai_user_affection SET affection = %s WHERE user_id = %s",
                (updated, user_id),
            )
        else:
            await connection.exec_driver_sql(
                "INSERT INTO ai_user_affection (user_id, affection) VALUES (%s, %s)",
                (user_id, updated),
            )

    return updated


def update_user_affection_sync(user_id: int, delta: int) -> int:
    return mysql_connection.run_sync(update_user_affection(user_id, delta))


async def async_get_user_affection(user_id: int) -> int:
    return await get_user_affection(user_id)


async def async_update_user_affection(user_id: int, delta: int) -> int:
    return await update_user_affection(user_id, delta)


async def get_user_permission(user_id: int) -> int:
    row = await mysql_connection.fetch_one(
        "SELECT permission FROM user WHERE id = %s",
        (user_id,),
    )
    return row[0] if row else 0


async def async_get_user_permission(user_id: int) -> int:
    return await get_user_permission(user_id)


async def async_update_user_coins(user_id: int, amount: int):
    """待移除：改用 `balance.credit` / `balance.debit`。返回值同 `update_user_coins`。"""
    return await update_user_coins(user_id, amount)


async def get_user_impression(user_id: int) -> str:
    row = await mysql_connection.fetch_one(
        "SELECT impression FROM ai_user_affection WHERE user_id = %s",
        (user_id,),
    )
    if row and row[0] is not None:
        return row[0]
    return ""


async def update_user_impression(user_id: int, impression: str) -> str:
    text = (impression or "").strip()
    async with mysql_connection.transaction() as connection:
        row = await mysql_connection.fetch_one(
            "SELECT impression FROM ai_user_affection WHERE user_id = %s",
            (user_id,),
            connection=connection,
        )
        if row:
            await connection.exec_driver_sql(
                "UPDATE ai_user_affection SET impression = %s WHERE user_id = %s",
                (text, user_id),
            )
        else:
            await connection.exec_driver_sql(
                "INSERT INTO ai_user_affection (user_id, affection, impression) VALUES (%s, %s, %s)",
                (user_id, 0, text),
            )
    return text


def update_user_impression_sync(user_id: int, impression: str) -> str:
    return mysql_connection.run_sync(update_user_impression(user_id, impression))


async def async_get_user_impression(user_id: int) -> str:
    return await get_user_impression(user_id)
