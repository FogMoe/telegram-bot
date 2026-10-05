"""入群验证记录：按欢迎消息认领、按到期时间恢复（真实 MySQL）。"""

from datetime import datetime, timedelta

from mysql_support import fetch_scalar, run

from fogmoe_telegram_bot.features.moderation import member_verify

CHAT = -100
USER = 42


def test_only_the_current_welcome_message_can_claim_a_round(app_database):
    later = datetime.now() + timedelta(minutes=5)
    run(member_verify.save_verification_task(USER, CHAT, 1, later))
    # 重新入群：同一个 (用户, 群组) 换成新的欢迎消息
    run(member_verify.save_verification_task(USER, CHAT, 2, later))

    assert run(member_verify.claim_verification_task(USER, CHAT, 1)) is False
    assert run(member_verify.find_verification_message(USER, CHAT)) == 2
    assert run(member_verify.claim_verification_task(USER, CHAT, 2)) is True
    # 同一轮只能被认领一次
    assert run(member_verify.claim_verification_task(USER, CHAT, 2)) is False
    assert fetch_scalar(app_database, "SELECT COUNT(*) FROM verification_tasks") == 0


def test_due_rounds_are_selected_by_expire_time(app_database):
    now = datetime.now()
    run(member_verify.save_verification_task(USER, CHAT, 1, now - timedelta(seconds=1)))
    run(member_verify.save_verification_task(7, CHAT, 2, now + timedelta(minutes=5)))

    rows = run(member_verify.due_verification_tasks(now))

    assert [tuple(row) for row in rows] == [(USER, CHAT, 1)]
