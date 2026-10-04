"""任务奖励（/task）的业务操作：任务定义与领取。不依赖 Telegram。

领取是一个事务：先锁用户行，确认没有完成记录，奖励入账与完成记录一起提交；用户不存在时不会留下完成记录。
"""

from enum import StrEnum

from core import balance, sql

from ..repositories import tasks as tasks_repository

# 任务ID
TASK_ID_CHECK_GROUP1 = 1  # 任务1：加入 @ScarletKc_Group 群组
TASK_ID_CHECK_GROUP2 = 2  # 任务2：加入 @FOG_MOE 群组

# 指定目标群组ID（使用群组ID，此格式适用于 Telegram API）
TARGET_GROUP_ID1 = -1001870858408  # 替换为 @ScarletKc_Group 实际群组 ID
TARGET_GROUP_ID2 = -1002053007005  # 替换为 @FOG_MOE 实际群组 ID

# 用于提示展示，可用群组用户名或名称
TASK_NAME_1 = "@ScarletKc_Group"
TASK_NAME_2 = "@FOG_MOE"

# 奖励硬币数，可根据需求设置
REWARD_COINS_1 = 10
REWARD_COINS_2 = 10


class TaskClaim(StrEnum):
    CLAIMED = "claimed"
    ALREADY_DONE = "already_done"
    NOT_REGISTERED = "not_registered"


def task_op_key(user_id: int, task_id: int) -> str:
    """任务奖励的身份：一个用户一个任务只有一次奖励（与 user_task 的主键一致）。"""
    return balance.make_op_key("task", user_id, task_id)


async def is_task_completed(user_id: int, task_id: int) -> bool:
    return await tasks_repository.is_completed(user_id, task_id)


async def claim_task_reward(user_id: int, task_id: int, reward_coins: int) -> TaskClaim:
    """奖励入账与完成记录在同一个事务里；用户不存在时不会留下完成记录。"""
    async with sql.transaction() as connection:
        # 先锁用户行，同一用户的并发领取在这里串行，之后的一致性读能看到上一个持锁者的完成记录。
        try:
            await balance.lock_user(connection, user_id)
        except balance.UserNotFound:
            return TaskClaim.NOT_REGISTERED
        if await tasks_repository.is_completed(user_id, task_id, connection=connection):
            return TaskClaim.ALREADY_DONE

        credit = await balance.credit(
            connection,
            user_id,
            reward_coins,
            op_key=task_op_key(user_id, task_id),
            reason="task",
        )
        await tasks_repository.record_completion(connection, user_id, task_id)
        # 完成记录被手工清掉但账本里已经有这笔奖励：补回完成记录，不再重复发放。
        return TaskClaim.CLAIMED if credit.applied else TaskClaim.ALREADY_DONE
