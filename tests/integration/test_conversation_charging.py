"""AI 对话扣费：扣费失败不进入本轮、不贡献奖池；同一 update 重放不重复扣费（真实 MySQL）。"""

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from economy_support import (
    Recorder,
    gather_all,
    ledger_keys,
    ledger_rows,
    make_command_update,
    make_context,
    pool_balance,
    pool_rows,
    seed_user,
    user_state,
)
from mysql_support import run

from core import command_cooldown
from features.conversation import batching, billing, handlers


def turn(message_id, cost, *, chat_id=100, edit_stamp=None, update_id=None):
    return billing.TurnMessage(
        chat_id=chat_id,
        message_id=message_id,
        cost=cost,
        edit_stamp=edit_stamp,
        update_id=update_id,
    )


class TestChargeTurn:
    def test_each_message_is_charged_under_its_own_identity_and_feeds_the_pool(
        self, app_database
    ):
        seed_user(app_database, 1, free=1, paid=9)

        charge = run(billing.charge_turn(1, [turn(10, 2), turn(11, 5)]))

        assert charge.status is billing.TurnChargeStatus.CHARGED
        assert charge.newly_charged == 7
        assert (charge.balance_free, charge.balance_paid) == (0, 3)
        assert ledger_keys(app_database) == ["chat:100:10", "chat:100:11"]
        assert [row["delta_free"] + row["delta_paid"] for row in ledger_rows(app_database)] == [
            -2,
            -5,
        ]
        assert pool_balance(app_database) == Decimal("1.40")
        assert [row["op_key"] for row in pool_rows(app_database)] == [
            "pool:chat:100:10",
            "pool:chat:100:11",
        ]

    def test_the_same_messages_delivered_again_are_not_charged_again(self, app_database):
        seed_user(app_database, 1, free=10)
        run(billing.charge_turn(1, [turn(10, 2), turn(11, 1)]))

        again = run(billing.charge_turn(1, [turn(10, 2), turn(11, 1)]))

        assert again.status is billing.TurnChargeStatus.CHARGED
        assert again.newly_charged == 0
        assert user_state(app_database, 1)["free"] == 7
        assert len(ledger_rows(app_database)) == 2
        assert pool_balance(app_database) == Decimal("0.60")

    def test_a_redelivered_message_in_a_different_batch_composition_is_charged_once(
        self, app_database
    ):
        seed_user(app_database, 1, free=10)
        run(billing.charge_turn(1, [turn(10, 2), turn(11, 1)]))

        # 重新投递后只剩第二条消息，加上一条新消息：只有新消息要付费。
        later = run(billing.charge_turn(1, [turn(11, 1), turn(12, 3)]))

        assert later.newly_charged == 3
        assert user_state(app_database, 1)["free"] == 4
        assert ledger_keys(app_database) == ["chat:100:10", "chat:100:11", "chat:100:12"]

    def test_different_turns_are_charged_as_usual(self, app_database):
        seed_user(app_database, 1, free=10)

        run(billing.charge_turn(1, [turn(10, 1)]))
        run(billing.charge_turn(1, [turn(20, 1)]))

        assert user_state(app_database, 1)["free"] == 8

    def test_an_edit_is_a_new_charge_but_a_redelivered_edit_is_not(self, app_database):
        seed_user(app_database, 1, free=10)
        run(billing.charge_turn(1, [turn(10, 1)]))

        run(billing.charge_turn(1, [turn(10, 1, edit_stamp=1_700_000_000)]))
        run(billing.charge_turn(1, [turn(10, 1, edit_stamp=1_700_000_000)]))
        run(billing.charge_turn(1, [turn(10, 1, edit_stamp=1_700_000_060)]))

        assert user_state(app_database, 1)["free"] == 7
        assert ledger_keys(app_database) == [
            "chat:100:10",
            "chat:100:10:edit:1700000000",
            "chat:100:10:edit:1700000060",
        ]

    def test_insufficient_balance_charges_nothing_and_adds_nothing_to_the_pool(
        self, app_database
    ):
        seed_user(app_database, 1, free=2)

        charge = run(billing.charge_turn(1, [turn(10, 1), turn(11, 2)]))

        assert charge.status is billing.TurnChargeStatus.INSUFFICIENT
        assert charge.total_cost == 3
        # 第一条消息本来付得起，但整轮回滚：什么都没扣，奖池也没有增加。
        assert user_state(app_database, 1)["free"] == 2
        assert ledger_rows(app_database) == []
        assert pool_balance(app_database) == Decimal("0")
        assert pool_rows(app_database) == []

    def test_unregistered_users_are_reported(self, app_database):
        charge = run(billing.charge_turn(404, [turn(10, 1)]))

        assert charge.status is billing.TurnChargeStatus.UNREGISTERED
        assert ledger_rows(app_database) == []
        assert pool_rows(app_database) == []

    def test_a_balance_spent_concurrently_stops_the_later_turn(self, app_database):
        seed_user(app_database, 1, free=1)

        async def scenario():
            return await gather_all(
                billing.charge_turn(1, [turn(10, 1)]),
                billing.charge_turn(1, [turn(11, 1)]),
                billing.charge_turn(1, [turn(12, 1)]),
            )

        results = run(scenario())

        assert all(not isinstance(item, Exception) for item in results), results
        statuses = sorted(item.status.value for item in results)
        assert statuses == ["charged", "insufficient", "insufficient"]
        assert user_state(app_database, 1)["free"] == 0
        assert len(ledger_rows(app_database)) == 1
        assert pool_balance(app_database) == Decimal("0.20")


# ---------------------------------------------------------------------------
# 通过真实 handler 驱动整轮对话：只替换 AI 与出站发送
# ---------------------------------------------------------------------------


@pytest.fixture
def conversation(monkeypatch):
    """把 `_reply_batch_unlocked` 里与扣费无关的外部依赖替换成替身，返回调用记录。"""
    ai_calls = []
    sent = Recorder(result=[])

    async def fake_ai_response(chat_history, user_id, **kwargs):
        ai_calls.append((user_id, len(chat_history)))
        return "你好呀", []

    async def no_op(*args, **kwargs):
        return None

    async def fake_reply_sender(**kwargs):
        sent.calls.append(((), kwargs))
        return []

    async def fake_normalize(text, **kwargs):
        return text

    async def fake_generated_media(**kwargs):
        return []

    monkeypatch.setattr(handlers.ai_chat, "get_ai_response", fake_ai_response)
    monkeypatch.setattr(handlers.idle_followup, "arm_from_private_turn", no_op)
    monkeypatch.setattr(handlers, "send_ai_reply_with_stickers", fake_reply_sender)
    monkeypatch.setattr(handlers, "normalize_sticker_directives", fake_normalize)
    monkeypatch.setattr(handlers, "send_generated_media", fake_generated_media)
    # 聊天冷却是进程内状态，并发的两条消息会被它挡掉，这里只关心数据库层的扣费。
    async def always_allowed(update):
        return True

    monkeypatch.setattr(command_cooldown, "check_chat_cooldown", always_allowed)
    return SimpleNamespace(ai_calls=ai_calls, sent=sent)


def chat_update(user_id, message_id, *, text="你好", edited_at=None, update_id=None):
    update = make_command_update(
        user_id=user_id, message_id=message_id, text=text, update_id=update_id
    )
    if edited_at is not None:
        message = update.message
        message.edit_date = edited_at
        update.edited_message = message
        update.message = None
    return update


def drive(update):
    context = make_context()
    return handlers._reply_batch_unlocked([batching._QueuedUpdate(update=update, context=context)])


def reply_texts(update):
    message = update.message or update.edited_message
    return message.reply_text.texts


class TestConversationTurn:
    def test_a_charged_turn_reaches_the_model_and_feeds_the_pool(
        self, app_database, conversation
    ):
        seed_user(app_database, 1, free=3)
        update = chat_update(1, 50)

        run(drive(update))

        assert len(conversation.ai_calls) == 1
        assert user_state(app_database, 1)["free"] == 2
        assert ledger_keys(app_database) == ["chat:1:50"]
        assert pool_balance(app_database) == Decimal("0.20")

    def test_an_empty_wallet_stops_the_turn_before_the_model_and_the_pool(
        self, app_database, conversation
    ):
        seed_user(app_database, 1, free=0)
        update = chat_update(1, 50)

        run(drive(update))

        assert conversation.ai_calls == []
        assert any("硬币不足" in text for text in reply_texts(update))
        assert ledger_rows(app_database) == []
        assert pool_balance(app_database) == Decimal("0")

    def test_a_balance_spent_after_the_first_look_does_not_let_the_second_turn_through(
        self, app_database, conversation
    ):
        # 两条消息几乎同时到达，余额只够其中一条：另一条必须在扣费处被拦住。
        seed_user(app_database, 1, free=1)
        first, second = chat_update(1, 50), chat_update(1, 51)

        async def scenario():
            await gather_all(drive(first), drive(second))

        run(scenario())

        assert len(conversation.ai_calls) == 1
        blocked = [update for update in (first, second) if any("硬币不足" in t for t in reply_texts(update))]
        assert len(blocked) == 1
        assert user_state(app_database, 1)["free"] == 0
        assert len(ledger_rows(app_database)) == 1
        assert pool_balance(app_database) == Decimal("0.20")

    def test_the_same_update_delivered_twice_is_charged_once(self, app_database, conversation):
        seed_user(app_database, 1, free=5)

        run(drive(chat_update(1, 50, update_id=900)))
        run(drive(chat_update(1, 50, update_id=900)))

        assert user_state(app_database, 1)["free"] == 4
        assert ledger_keys(app_database) == ["chat:1:50"]
        assert pool_balance(app_database) == Decimal("0.20")
        # 重复投递几乎总是发生在进程中途被杀之后：这一轮仍然继续处理，只是不再收钱。
        assert len(conversation.ai_calls) == 2

    def test_a_new_message_and_an_edit_are_both_charged(self, app_database, conversation):
        seed_user(app_database, 1, free=5)
        edited_at = datetime(2026, 10, 5, 8, 0, 0, tzinfo=timezone.utc)

        run(drive(chat_update(1, 50)))
        run(drive(chat_update(1, 50, edited_at=edited_at)))

        assert user_state(app_database, 1)["free"] == 3
        assert ledger_keys(app_database) == [
            "chat:1:50",
            f"chat:1:50:edit:{int(edited_at.timestamp())}",
        ]

    def test_a_batch_of_messages_is_charged_per_message_in_one_transaction(
        self, app_database, conversation
    ):
        seed_user(app_database, 1, free=4)
        first = chat_update(1, 50)
        second = chat_update(1, 51, text="长" * 150)  # 101-500 字符：2 个币
        context = make_context()

        run(
            handlers._reply_batch_unlocked(
                [
                    batching._QueuedUpdate(update=first, context=context),
                    batching._QueuedUpdate(update=second, context=context),
                ]
            )
        )

        assert len(conversation.ai_calls) == 1
        assert user_state(app_database, 1)["free"] == 1
        assert ledger_keys(app_database) == ["chat:1:50", "chat:1:51"]
        assert pool_balance(app_database) == Decimal("0.60")
