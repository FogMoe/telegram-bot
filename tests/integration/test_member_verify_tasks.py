"""入群验证记录：按欢迎消息认领、按到期时间恢复、机器人离群时清理（真实 MySQL）。"""

from datetime import datetime, timedelta

from mysql_support import fetch_scalar, run

from fogmoe_telegram_bot.core import sql
from fogmoe_telegram_bot.features.moderation.operations import member_verify as verification
from fogmoe_telegram_bot.features.moderation.repositories import verification as verification_tasks
from fogmoe_telegram_bot.features.moderation.repositories.verification import VerificationTask

CHAT = -100
USER = 42


def test_only_the_current_welcome_message_can_claim_a_round(app_database):
    run(verification.open_round(USER, CHAT, 1))
    # 重新入群：同一个 (用户, 群组) 换成新的欢迎消息
    run(verification.open_round(USER, CHAT, 2))

    assert run(verification.claim_round(USER, CHAT, 1)) is False
    assert run(verification.find_round_message(USER, CHAT)) == 2
    assert run(verification.claim_round(USER, CHAT, 2)) is True
    # 同一轮只能被认领一次
    assert run(verification.claim_round(USER, CHAT, 2)) is False
    assert fetch_scalar(app_database, "SELECT COUNT(*) FROM verification_tasks") == 0


async def _save(user_id, message_id, expire_time):
    async with sql.transaction() as connection:
        await verification_tasks.save_task(connection, user_id, CHAT, message_id, expire_time)


def test_due_rounds_are_selected_by_expire_time(app_database):
    now = datetime.now()
    run(_save(USER, 1, now - timedelta(seconds=1)))
    run(_save(7, 2, now + timedelta(minutes=5)))

    assert run(verification.due_rounds()) == [VerificationTask(USER, CHAT, 1)]


def test_a_removed_bot_forgets_the_group(app_database):
    run(verification.enable(CHAT, None))
    run(verification.open_round(USER, CHAT, 1))

    run(verification.forget_group(CHAT))

    assert run(verification.is_enabled(CHAT)) is False
    assert fetch_scalar(app_database, "SELECT COUNT(*) FROM verification_tasks") == 0
