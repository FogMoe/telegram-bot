"""/spam ai 的付费、续期与到期提醒（真实 MySQL）：扣款与有效期同事务，续期叠加在原到期时间上，提醒每个到期时间只发一次。"""

from economy_support import ledger_rows, seed_user, total_coins
from mysql_support import execute, fetch, fetch_scalar, run

from fogmoe_telegram_bot.core import sql
from fogmoe_telegram_bot.features.moderation.spam_ai import operations
from fogmoe_telegram_bot.features.moderation.spam_ai.operations import PayStatus
from fogmoe_telegram_bot.features.moderation.spam_ai.repositories import groups
from fogmoe_telegram_bot.features.moderation.spam_ai.repositories.groups import ReminderKind

CHAT = -1001


def enable(user_id=1, message_id=5, chat_id=CHAT):
    return run(operations.enable(chat_id, user_id, operations.payment_op_key(chat_id, message_id)))


def renew(user_id=1, message_id=6, chat_id=CHAT):
    return run(operations.renew(chat_id, user_id, operations.payment_op_key(chat_id, message_id)))


def hours_left(url, chat_id=CHAT):
    return fetch_scalar(
        url,
        "SELECT TIMESTAMPDIFF(HOUR, UTC_TIMESTAMP(6), paid_until) FROM group_spam_ai WHERE chat_id = %s",
        (chat_id,),
    )


def set_paid_until(url, expression, chat_id=CHAT):
    execute(url, (f"UPDATE group_spam_ai SET paid_until = {expression} WHERE chat_id = %s", (chat_id,)))


def test_the_first_payment_charges_once_for_thirty_days(app_database):
    seed_user(app_database, 1, free=150)

    result = enable()

    assert result.status is PayStatus.CHARGED and not result.extended
    assert total_coins(app_database, 1) == 50
    rows = ledger_rows(app_database, 1)
    assert [(row["op_key"], row["reason"], row["ref"]) for row in rows] == [
        ("spamai:-1001:5", "spam_ai", "chat:-1001")
    ]
    assert hours_left(app_database) in (719, 720)


def test_renewing_early_stacks_and_a_replayed_command_is_free(app_database):
    seed_user(app_database, 1, free=300)
    enable()

    first = renew()
    replay = renew()

    assert first.status is PayStatus.CHARGED and first.extended
    assert replay.status is PayStatus.CHARGED
    assert total_coins(app_database, 1) == 100
    assert hours_left(app_database) in (1439, 1440)


def test_an_expired_group_restarts_from_now(app_database):
    seed_user(app_database, 1, free=200)
    enable()
    set_paid_until(app_database, "UTC_TIMESTAMP(6) - INTERVAL 5 DAY")

    result = enable(message_id=7)

    assert result.status is PayStatus.CHARGED and not result.extended
    assert total_coins(app_database, 1) == 0
    assert hours_left(app_database) in (719, 720)


def test_pausing_keeps_the_expiry_and_resuming_is_free(app_database):
    seed_user(app_database, 1, free=100)
    enable()

    paused = run(operations.pause(CHAT))
    paused_again = run(operations.pause(CHAT))
    resumed = enable(message_id=8)

    assert paused is not None and not paused.enabled
    assert paused_again is None
    assert resumed.status is PayStatus.RESUMED
    assert total_coins(app_database, 1) == 0
    assert hours_left(app_database) in (719, 720)


def test_insufficient_balance_or_unregistered_user_changes_nothing(app_database):
    seed_user(app_database, 1, free=99)

    poor = enable()
    stranger = enable(user_id=404, message_id=6)

    assert poor.status is PayStatus.INSUFFICIENT and poor.balance_total == 99
    assert stranger.status is PayStatus.NOT_REGISTERED
    assert fetch(app_database, "SELECT chat_id FROM group_spam_ai") == []
    assert ledger_rows(app_database) == []


def due(kind):
    return [reminder.chat_id for reminder in run(operations.due_reminders(kind))]


def test_reminders_are_claimed_once_per_expiry(app_database):
    seed_user(app_database, 1, free=500)
    for chat_id in (-1, -2, -3):
        enable(chat_id=chat_id)
    set_paid_until(app_database, "UTC_TIMESTAMP(6) + INTERVAL 2 DAY", chat_id=-1)
    set_paid_until(app_database, "UTC_TIMESTAMP(6) - INTERVAL 1 HOUR", chat_id=-2)
    set_paid_until(app_database, "UTC_TIMESTAMP(6) - INTERVAL 1 HOUR", chat_id=-3)
    execute(app_database, "UPDATE group_spam_ai SET enabled = 0 WHERE chat_id = -3")

    assert due(ReminderKind.EXPIRING) == [-1]
    assert due(ReminderKind.EXPIRED) == [-2]  # 暂停中的群不提醒

    (expiring,) = run(operations.due_reminders(ReminderKind.EXPIRING))
    assert run(operations.claim_reminder(ReminderKind.EXPIRING, expiring))
    assert not run(operations.claim_reminder(ReminderKind.EXPIRING, expiring))
    assert due(ReminderKind.EXPIRING) == []

    # 续费改变了到期时间：新的到期时间临近时会再提醒
    renew(chat_id=-1, message_id=9)
    set_paid_until(app_database, "UTC_TIMESTAMP(6) + INTERVAL 1 DAY", chat_id=-1)
    assert due(ReminderKind.EXPIRING) == [-1]


def test_a_group_upgrade_moves_the_paid_state_and_counts(app_database):
    seed_user(app_database, 1, free=100)
    enable()
    new_chat = -100999

    async def count_one():
        async with sql.transaction() as connection:
            await groups.add_checked(connection, CHAT, 7)

    run(count_one())

    assert run(operations.move_group(CHAT, new_chat))
    assert not run(operations.move_group(CHAT, new_chat))
    assert fetch(app_database, "SELECT chat_id FROM group_spam_ai") == [{"chat_id": new_chat}]
    assert fetch(app_database, "SELECT chat_id, user_id, checked_count FROM group_spam_ai_members") == [
        {"chat_id": new_chat, "user_id": 7, "checked_count": 1}
    ]
    assert hours_left(app_database, chat_id=new_chat) in (719, 720)


def test_member_check_counts_accumulate_and_reset(app_database):
    async def scenario():
        async with sql.transaction() as connection:
            counts = [await groups.add_checked(connection, CHAT, 7) for _ in range(3)]
        async with sql.transaction() as connection:
            await groups.forget_member(connection, CHAT, 7)
        return counts, await groups.get_checked_count(CHAT, 7)

    counts, after_forget = run(scenario())

    assert counts == [1, 2, 3]
    assert after_forget == 0
