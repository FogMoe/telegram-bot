import random
from datetime import datetime, timedelta
from . import mysql_connection
import asyncio
from concurrent.futures import ThreadPoolExecutor

# 创建线程池执行器用于异步数据库操作
user_executor = ThreadPoolExecutor(max_workers=5)

# 添加用户抽奖锁字典，防止同一用户并发抽奖
lottery_locks = {}

def get_user_last_lottery_date(user_id):
    connection = mysql_connection.create_connection()
    cursor = connection.cursor()
    select_query = "SELECT last_lottery_date FROM user_lottery WHERE user_id = %s"
    cursor.execute(select_query, (user_id,))
    result = cursor.fetchone()
    cursor.close()
    connection.close()
    return result[0] if result else None


def update_user_lottery_date(user_id):
    connection = mysql_connection.create_connection()
    cursor = connection.cursor()
    update_query = "INSERT INTO user_lottery (user_id, last_lottery_date) VALUES (%s, %s) ON DUPLICATE KEY UPDATE last_lottery_date = VALUES(last_lottery_date)"
    cursor.execute(update_query, (user_id, datetime.now()))
    connection.commit()
    cursor.close()
    connection.close()


def update_user_coins(user_id, coins):
    connection = mysql_connection.create_connection()
    cursor = connection.cursor()
    update_query = "UPDATE user SET coins = coins + %s WHERE id = %s"
    cursor.execute(update_query, (coins, user_id))
    connection.commit()
    cursor.close()
    connection.close()


def user_exists(user_id):
    connection = mysql_connection.create_connection()
    cursor = connection.cursor()
    select_query = "SELECT id FROM user WHERE id = %s"
    cursor.execute(select_query, (user_id,))
    result = cursor.fetchone()
    cursor.close()
    connection.close()
    return result is not None


async def async_user_exists(user_id):
    """异步检查用户是否存在"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        user_executor,
        lambda: user_exists(user_id)
    )


def lottery(user_id):
    if not user_exists(user_id):
        return ("请先使用 /me 命令获取个人信息。\n"
                "Please register first using the /me command.")

    last_lottery_date = get_user_last_lottery_date(user_id)
    if last_lottery_date and datetime.now() - last_lottery_date < timedelta(hours=24):
        return ("每24小时您只能参加一次抽奖喵。下次再来吧！\n"
                "You can only participate in the lottery once every 24 hours. Meow! Come back later!")

    # Define the lottery probabilities
    probabilities = [0.1, 0.1, 0.8]
    coins_distribution = [random.choices(range(0, 10), k=1)[0], random.choices(range(20, 30), k=1)[0],
                          random.choices(range(10, 20), k=1)[0]]
    coins = random.choices(coins_distribution, probabilities)[0]

    # Update the user's coins and last lottery date
    update_user_coins(user_id, coins)
    update_user_lottery_date(user_id)

    return (f"恭喜！您赢得了 {coins} 枚硬币喵。\n"
            f"Congratulations! You have won {coins} coins. Meow!")


async def async_lottery(user_id):
    """异步版本的抽奖函数，添加锁以防止同一用户并发抽奖"""
    # 检查用户是否已在抽奖
    if user_id in lottery_locks:
        return "抽奖操作过于频繁，请等待上一次操作完成。\nYou're drawing too fast, please wait for the previous lottery to complete."
        
    try:
        # 标记用户正在进行抽奖操作
        lottery_locks[user_id] = True
        
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            user_executor,
            lambda: lottery(user_id)
        )
        return result
    finally:
        # 无论抽奖成功与否，都移除用户标记
        if user_id in lottery_locks:
            del lottery_locks[user_id]


def get_user_personal_info(user_id: int) -> str:
    """从数据库中获取用户的个人信息。如果记录不存在或数据库里是NULL则返回空字符串。"""
    user_info="User-defined personal information: "
    connection = mysql_connection.create_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT info FROM user WHERE id = %s", (user_id,))
        result = cursor.fetchone()
        if not result or result[0] is None or result[0] == "":
            return ""
        user_info += result[0] + ""
        return user_info
    finally:
        cursor.close()
        connection.close()


async def async_get_user_personal_info(user_id: int) -> str:
    """异步获取用户个人信息"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        user_executor,
        lambda: get_user_personal_info(user_id)
    )


def get_user_coins(user_id: int) -> int:
    """从数据库中获取用户的硬币数，若无记录则返回0。"""
    connection = mysql_connection.create_connection()
    cursor = connection.cursor()
    select_query = "SELECT coins FROM user WHERE id = %s"
    cursor.execute(select_query, (user_id,))
    result = cursor.fetchone()
    cursor.close()
    connection.close()
    if result:
        return result[0]
    return 0


async def async_get_user_coins(user_id: int) -> int:
    """异步获取用户硬币数"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        user_executor,
        lambda: get_user_coins(user_id)
    )


def get_user_affection(user_id: int) -> int:
    """获取AI对用户的好感度，未记录则返回0。"""
    connection = mysql_connection.create_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT affection FROM ai_user_affection WHERE user_id = %s",
            (user_id,),
        )
        result = cursor.fetchone()
        if result:
            return result[0]
        return 0
    finally:
        cursor.close()
        connection.close()


def update_user_affection(user_id: int, delta: int) -> int:
    """调整并返回AI对用户的好感度，总值限制[-100, 100]，单次变动[-10, 10]。"""
    delta = int(delta)
    if delta > 10:
        delta = 10
    elif delta < -10:
        delta = -10
    connection = mysql_connection.create_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT affection FROM ai_user_affection WHERE user_id = %s FOR UPDATE",
            (user_id,),
        )
        row = cursor.fetchone()
        current = row[0] if row else 0
        updated = max(-100, min(100, current + delta))

        if row:
            cursor.execute(
                "UPDATE ai_user_affection SET affection = %s WHERE user_id = %s",
                (updated, user_id),
            )
        else:
            cursor.execute(
                "INSERT INTO ai_user_affection (user_id, affection) VALUES (%s, %s)",
                (user_id, updated),
            )

        connection.commit()
        return updated
    finally:
        cursor.close()
        connection.close()


async def async_get_user_affection(user_id: int) -> int:
    """异步获取好感度。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        user_executor,
        lambda: get_user_affection(user_id)
    )


async def async_update_user_affection(user_id: int, delta: int) -> int:
    """异步调整好感度并返回最新数值。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        user_executor,
        lambda: update_user_affection(user_id, delta)
    )


def get_user_permission(user_id: int) -> int:
    """
    从数据库中获取用户权限等级，若不存在则返回0
    """
    connection = mysql_connection.create_connection()
    cursor = connection.cursor()
    try:
        select_query = "SELECT permission FROM user WHERE id = %s"
        cursor.execute(select_query, (user_id,))
        result = cursor.fetchone()
        if result:
            return result[0]
        return 0
    finally:
        cursor.close()
        connection.close()


async def async_get_user_permission(user_id: int) -> int:
    """异步获取用户权限等级"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        user_executor,
        lambda: get_user_permission(user_id)
    )

async def async_update_user_coins(user_id: int, amount: int) -> None:
    """异步更新用户硬币数量"""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        user_executor,
        lambda: update_user_coins(user_id, amount)
    )


def get_user_impression(user_id: int) -> str:
    """获取AI对用户的印象文本。"""
    connection = mysql_connection.create_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT impression FROM ai_user_affection WHERE user_id = %s",
            (user_id,),
        )
        result = cursor.fetchone()
        if result and result[0] is not None:
            return result[0]
        return ""
    finally:
        cursor.close()
        connection.close()


def update_user_impression(user_id: int, impression: str) -> str:
    """更新AI对用户的印象文本并返回最新内容。"""
    text = (impression or "").strip()
    connection = mysql_connection.create_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT impression FROM ai_user_affection WHERE user_id = %s",
            (user_id,),
        )
        row = cursor.fetchone()
        if row:
            cursor.execute(
                "UPDATE ai_user_affection SET impression = %s WHERE user_id = %s",
                (text, user_id),
            )
        else:
            cursor.execute(
                "INSERT INTO ai_user_affection (user_id, affection, impression) VALUES (%s, %s, %s)",
                (user_id, 0, text),
            )
        connection.commit()
        return text
    finally:
        cursor.close()
        connection.close()


async def async_get_user_impression(user_id: int) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        user_executor,
        lambda: get_user_impression(user_id)
    )


async def async_update_user_impression(user_id: int, impression: str) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        user_executor,
        lambda: update_user_impression(user_id, impression)
    )
