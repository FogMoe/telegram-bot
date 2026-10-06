"""/xfeed 的开通、换绑与同步进度（真实 MySQL）：开通费每个群只扣一次，扣款与开通记录同事务。"""

from economy_support import ledger_rows, seed_user, total_coins
from mysql_support import fetch, run

from fogmoe_telegram_bot.features.xfeed import operations
from fogmoe_telegram_bot.features.xfeed.operations import BindRequest, BindStatus
from fogmoe_telegram_bot.features.xfeed.repositories.feeds import ActiveFeed

CHAT = -1001


def bind(user_id=1, handle="alice", latest=100, message_id=5, chat_id=CHAT):
    request = BindRequest(
        chat_id=chat_id,
        user_id=user_id,
        handle=handle,
        latest_post_id=latest,
        op_key=operations.activation_op_key(chat_id, message_id),
    )
    return run(operations.bind_feed(request))


def feed_row(url, chat_id=CHAT):
    rows = fetch(
        url,
        "SELECT x_handle, enabled, last_seen_id, paid_by, paid_op_key FROM group_x_feeds "
        "WHERE chat_id = %s",
        (chat_id,),
    )
    return rows[0] if rows else None


def test_first_bind_charges_once_and_records_the_activation(app_database):
    seed_user(app_database, 1, free=150)

    result = bind()

    assert result.status is BindStatus.BOUND and result.charged
    assert total_coins(app_database, 1) == 50
    assert [row["op_key"] for row in ledger_rows(app_database, 1)] == ["xfeed:-1001:5"]
    assert feed_row(app_database) == {
        "x_handle": "alice",
        "enabled": 1,
        "last_seen_id": 100,
        "paid_by": 1,
        "paid_op_key": "xfeed:-1001:5",
    }


def test_switching_accounts_and_a_replayed_command_are_free(app_database):
    seed_user(app_database, 1, free=150)
    seed_user(app_database, 2)  # 另一个管理员，没有金币
    bind()

    replay = bind()
    switched = bind(user_id=2, handle="bob", latest=300, message_id=6)

    assert not replay.charged and not switched.charged
    assert switched.replaced_handle == "alice"
    assert total_coins(app_database, 1) == 50
    assert feed_row(app_database)["x_handle"] == "bob"
    assert feed_row(app_database)["last_seen_id"] == 300


def test_insufficient_balance_or_unregistered_user_changes_nothing(app_database):
    seed_user(app_database, 1, free=99)

    poor = bind()
    stranger = bind(user_id=404, message_id=6)

    assert poor.status is BindStatus.INSUFFICIENT and poor.balance_total == 99
    assert stranger.status is BindStatus.NOT_REGISTERED
    assert feed_row(app_database) is None
    assert ledger_rows(app_database) == []


def test_unbind_pauses_and_rebinding_the_same_account_resumes_its_progress(app_database):
    seed_user(app_database, 1, free=100)
    bind(latest=100)
    feed = ActiveFeed(CHAT, "alice", 100)
    run(operations.record_delivered(feed, 150))
    run(operations.record_delivered(feed, 120))  # 进度只往前走
    assert feed_row(app_database)["last_seen_id"] == 150

    assert run(operations.unbind_feed(CHAT)) is True
    assert run(operations.unbind_feed(CHAT)) is False
    assert feed_row(app_database)["enabled"] == 0
    assert run(operations.active_feeds()) == []

    resumed = bind(handle="ALICE", latest=999, message_id=7)

    assert resumed.status is BindStatus.BOUND and resumed.resumed and not resumed.charged
    assert feed_row(app_database)["last_seen_id"] == 150
    assert run(operations.active_feeds()) == [ActiveFeed(CHAT, "ALICE", 150)]
    assert total_coins(app_database, 1) == 0


def test_a_paused_group_bound_to_another_account_starts_from_its_latest_post(app_database):
    seed_user(app_database, 1, free=100)
    bind(latest=100)
    run(operations.unbind_feed(CHAT))

    result = bind(handle="carol", latest=500, message_id=6)

    assert not result.resumed and result.replaced_handle is None
    assert feed_row(app_database)["last_seen_id"] == 500


def test_a_migrated_group_keeps_its_activation(app_database):
    seed_user(app_database, 1, free=100)
    bind()

    assert run(operations.move_feed(CHAT, -1002)) is True

    assert feed_row(app_database) is None
    assert feed_row(app_database, -1002)["x_handle"] == "alice"
