import random
from datetime import datetime, timedelta

from . import mysql_connection

# 添加用户抽奖锁字典，防止同一用户并发抽奖
lottery_locks = {}


async def get_user_last_lottery_date(user_id):
    row = await mysql_connection.fetch_one(
        "SELECT last_lottery_date FROM user_lottery WHERE user_id = %s",
        (user_id,),
    )
    return row[0] if row else None


async def update_user_lottery_date(user_id):
    await mysql_connection.execute(
        "INSERT INTO user_lottery (user_id, last_lottery_date) VALUES (%s, %s) "
        "ON DUPLICATE KEY UPDATE last_lottery_date = VALUES(last_lottery_date)",
        (user_id, datetime.now()),
    )


async def update_user_coins(user_id, coins):
    await mysql_connection.execute(
        "UPDATE user SET coins = coins + %s WHERE id = %s",
        (coins, user_id),
    )


async def user_exists(user_id):
    row = await mysql_connection.fetch_one(
        "SELECT id FROM user WHERE id = %s",
        (user_id,),
    )
    return row is not None


def user_exists_sync(user_id):
    return mysql_connection.run_sync(user_exists(user_id))


async def async_user_exists(user_id):
    return await user_exists(user_id)


async def lottery(user_id):
    if not await user_exists(user_id):
        return (
            "请先使用 /me 命令获取个人信息。\n"
            "Please register first using the /me command."
        )

    last_lottery_date = await get_user_last_lottery_date(user_id)
    if last_lottery_date and datetime.now() - last_lottery_date < timedelta(hours=24):
        return (
            "每24小时您只能参加一次抽奖喵。下次再来吧！\n"
            "You can only participate in the lottery once every 24 hours. Meow! Come back later!"
        )

    probabilities = [0.1, 0.1, 0.8]
    coins_distribution = [
        random.choices(range(0, 10), k=1)[0],
        random.choices(range(20, 30), k=1)[0],
        random.choices(range(10, 20), k=1)[0],
    ]
    coins = random.choices(coins_distribution, probabilities)[0]

    await update_user_coins(user_id, coins)
    await update_user_lottery_date(user_id)

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
    return f"User-defined personal information: {row[0]}"


def get_user_personal_info_sync(user_id: int) -> str:
    return mysql_connection.run_sync(get_user_personal_info(user_id))


async def async_get_user_personal_info(user_id: int) -> str:
    return await get_user_personal_info(user_id)


async def get_user_coins(user_id: int) -> int:
    row = await mysql_connection.fetch_one(
        "SELECT coins FROM user WHERE id = %s",
        (user_id,),
    )
    return row[0] if row else 0


def get_user_coins_sync(user_id: int) -> int:
    return mysql_connection.run_sync(get_user_coins(user_id))


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


def get_user_permission_sync(user_id: int) -> int:
    return mysql_connection.run_sync(get_user_permission(user_id))


async def async_get_user_permission(user_id: int) -> int:
    return await get_user_permission(user_id)


async def async_update_user_coins(user_id: int, amount: int) -> None:
    await update_user_coins(user_id, amount)


async def get_user_impression(user_id: int) -> str:
    row = await mysql_connection.fetch_one(
        "SELECT impression FROM ai_user_affection WHERE user_id = %s",
        (user_id,),
    )
    if row and row[0] is not None:
        return row[0]
    return ""


def get_user_impression_sync(user_id: int) -> str:
    return mysql_connection.run_sync(get_user_impression(user_id))


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


async def async_update_user_impression(user_id: int, impression: str) -> str:
    return await update_user_impression(user_id, impression)
