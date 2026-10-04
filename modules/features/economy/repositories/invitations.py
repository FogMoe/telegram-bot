"""邀请记录（`user_invitations`，以被邀请人为主键，所以同一个人只能被邀请一次）。

开户用 `core.user_records.create_user`，用户名读取用 `core.user_records.get_name`。
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncConnection

from core import sql


@dataclass(frozen=True, slots=True)
class InvitedUser:
    user_id: int
    name: str
    invited_at: datetime


@dataclass(frozen=True, slots=True)
class Referrer:
    user_id: int
    name: str


async def insert_invitation(
    connection: AsyncConnection, invited_user_id: int, referrer_id: int
) -> None:
    """登记邀请并标记奖励已发放。被邀请人已经有记录时抛 `IntegrityError`（重复邀请）。"""
    await connection.exec_driver_sql(
        "INSERT INTO user_invitations (invited_user_id, referrer_id, invitation_time, reward_claimed) "
        "VALUES (%s, %s, NOW(), TRUE)",
        (invited_user_id, referrer_id),
    )


async def count_invited(user_id: int, *, connection: AsyncConnection | None = None) -> int:
    row = await sql.fetch_one(
        "SELECT COUNT(*) FROM user_invitations WHERE referrer_id = %s",
        (user_id,),
        connection=connection,
    )
    return int(row[0]) if row else 0


async def list_invited(
    user_id: int, *, connection: AsyncConnection | None = None
) -> list[InvitedUser]:
    """邀请人邀请过的用户，最近的在前。"""
    rows = await sql.fetch_all(
        "SELECT i.invited_user_id, u.name, i.invitation_time "
        "FROM user_invitations i "
        "JOIN user u ON i.invited_user_id = u.id "
        "WHERE i.referrer_id = %s "
        "ORDER BY i.invitation_time DESC",
        (user_id,),
        connection=connection,
    )
    return [InvitedUser(int(row[0]), str(row[1]), row[2]) for row in rows]


async def get_referrer(
    user_id: int, *, connection: AsyncConnection | None = None
) -> Referrer | None:
    row = await sql.fetch_one(
        "SELECT ui.referrer_id, u.name "
        "FROM user_invitations ui "
        "JOIN user u ON ui.referrer_id = u.id "
        "WHERE ui.invited_user_id = %s",
        (user_id,),
        connection=connection,
    )
    return None if row is None else Referrer(int(row[0]), str(row[1]))
