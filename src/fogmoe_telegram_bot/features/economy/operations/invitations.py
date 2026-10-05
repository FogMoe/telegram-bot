"""邀请奖励（/ref、/start 邀请链接）的业务操作：登记邀请并给双方发放奖励。不依赖 Telegram。

开户（需要时）、邀请记录、双方的邀请奖励在同一个事务里；先按 user id 升序锁住双方，再写邀请记录。
`user_invitations` 的主键是被邀请人，重复邀请由唯一键拒绝（此时还没有任何奖励入账）；奖励的 op_key
由被邀请人派生，同一个被邀请人的奖励即使被重放也只入账一次。查询类函数出错时记日志并返回空结果，
邀请信息的展示不因数据库故障而中断。
"""

import logging
from dataclasses import dataclass
from typing import NamedTuple

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_telegram_bot.core import balance, config, sql, user_records

from ..repositories import invitations as invitations_repository
from ..repositories.invitations import InvitedUser, Referrer

logger = logging.getLogger(__name__)

# 邀请奖励的金币数量（邀请人与被邀请人各得一份）
INVITATION_REWARD = 20


def invited_user_reward() -> int:
    """新用户经邀请开户时拿到的总额：邀请奖励加新人奖励（新人奖励在调用时读配置）。"""
    return INVITATION_REWARD + config.NEW_USER_BONUS_COINS


class InvitationOutcome(NamedTuple):
    """`add_invitation_record` 的结果；重复邀请、邀请人不存在、数据库错误都是 (False, False)。"""

    recorded: bool
    new_user: bool  # 被邀请人是否是这次新开的户


class _InvitedUserChanged(Exception):
    """「被邀请人是否已有账户」的判断过期了（并发的开户或删除）：重新判断一次。"""


def invitee_op_key(invited_user_id: int) -> str:
    """被邀请人的邀请奖励：一个被邀请人只能被邀请一次，所以只入账一次。"""
    return balance.make_op_key("ref_invitee", invited_user_id)


def referrer_op_key(invited_user_id: int) -> str:
    """邀请人因这个被邀请人得到的奖励。"""
    return balance.make_op_key("ref_referrer", invited_user_id)


async def _record_invitation(
    connection: AsyncConnection,
    invited_user_id: int,
    referrer_id: int,
    invited_user_name: str,
    *,
    invited_exists: bool,
) -> InvitationOutcome:
    """在一个事务里完成：开户（需要时）、邀请记录、双方的邀请奖励。"""
    to_lock = [referrer_id]
    if invited_exists:
        to_lock.append(invited_user_id)
    try:
        await balance.lock_users(connection, to_lock)
    except balance.UserNotFound as exc:
        if exc.user_id == referrer_id:
            return InvitationOutcome(False, False)
        raise _InvitedUserChanged() from exc

    is_new_user = False
    if not invited_exists:
        # 与 /me 一样，新用户的开户奖励走账本（signup:<uid>）；邀请奖励另算。
        try:
            await user_records.create_user(connection, invited_user_id, invited_user_name)
        except IntegrityError as exc:
            if sql.is_duplicate_key_error(exc):
                raise _InvitedUserChanged() from exc
            raise
        is_new_user = True
        if config.NEW_USER_BONUS_COINS > 0:
            await balance.credit(
                connection,
                invited_user_id,
                config.NEW_USER_BONUS_COINS,
                op_key=balance.make_op_key("signup", invited_user_id),
                reason="signup",
            )

    try:
        await invitations_repository.insert_invitation(connection, invited_user_id, referrer_id)
    except IntegrityError as exc:
        if sql.is_duplicate_key_error(exc):
            return InvitationOutcome(False, False)  # 已经被邀请过：什么都没有写入
        raise

    await balance.credit(
        connection,
        invited_user_id,
        INVITATION_REWARD,
        op_key=invitee_op_key(invited_user_id),
        reason="ref_invitee",
        ref=f"referrer:{referrer_id}",
    )
    await balance.credit(
        connection,
        referrer_id,
        INVITATION_REWARD,
        op_key=referrer_op_key(invited_user_id),
        reason="ref_referrer",
        ref=f"invited:{invited_user_id}",
    )
    return InvitationOutcome(True, is_new_user)


async def add_invitation_record(
    invited_user_id: int, referrer_id: int, invited_user_name: str
) -> InvitationOutcome:
    """添加邀请记录到数据库，并给邀请人和被邀请人发放奖励。

    重复邀请、邀请人不存在、数据库错误都返回 (False, False)。
    """
    try:
        for _ in range(2):
            invited_exists = await user_records.check_user_exists(invited_user_id)
            try:
                return await balance.run_in_transaction(
                    lambda connection: _record_invitation(
                        connection,
                        invited_user_id,
                        referrer_id,
                        invited_user_name,
                        invited_exists=invited_exists,
                    )
                )
            except _InvitedUserChanged:
                continue
        return InvitationOutcome(False, False)
    except Exception as e:
        logger.error(f"Database error in add_invitation_record: {e}")
        return InvitationOutcome(False, False)


@dataclass(frozen=True, slots=True)
class InvitationSummary:
    count: int
    invited: list[InvitedUser]


async def get_invitation_summary(user_id: int) -> InvitationSummary:
    """用户邀请过的人数与名单（最近的在前）；出错时返回空结果。"""
    try:
        count = await invitations_repository.count_invited(user_id)
        invited = await invitations_repository.list_invited(user_id)
        return InvitationSummary(count, invited)
    except Exception as e:
        logger.error(f"Database error in get_invited_users: {e}")
        return InvitationSummary(0, [])


async def get_referrer(user_id: int) -> Referrer | None:
    """用户的邀请人；没有或出错时返回 None。"""
    try:
        return await invitations_repository.get_referrer(user_id)
    except Exception as e:
        logger.error(f"Database error in get_referrer: {e}")
        return None


async def get_user_name(user_id: int) -> str | None:
    """按用户 id 取用户名；不存在或出错时返回 None。"""
    try:
        return await user_records.get_name(user_id)
    except Exception as e:
        logger.error(f"Database error in get_user_name for user_id {user_id}: {e}")
        return None


async def user_exists(user_id: int) -> bool:
    """用户是否存在；出错时返回 False。"""
    try:
        return await user_records.check_user_exists(user_id)
    except Exception as e:
        logger.error(f"Database error in check_user_exists for user_id {user_id}: {e}")
        return False
