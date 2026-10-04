"""任务完成记录（`user_task`，主键 `(user_id, task_id)`：一个用户一个任务只有一条）。"""

from sqlalchemy.ext.asyncio import AsyncConnection

from core import sql


async def is_completed(
    user_id: int, task_id: int, *, connection: AsyncConnection | None = None
) -> bool:
    row = await sql.fetch_one(
        "SELECT 1 FROM user_task WHERE user_id = %s AND task_id = %s",
        (user_id, task_id),
        connection=connection,
    )
    return row is not None


async def record_completion(connection: AsyncConnection, user_id: int, task_id: int) -> None:
    await connection.exec_driver_sql(
        "INSERT INTO user_task (user_id, task_id) VALUES (%s, %s)",
        (user_id, task_id),
    )
