"""多人下注：轮次与下注持久化、回调的面板绑定、下注与结算串行、奖金只入账一次、重启恢复（真实 MySQL）。"""

import asyncio
from types import SimpleNamespace

import pytest
from economy_support import (
    Recorder,
    gather_all,
    ledger_rows,
    make_callback_update,
    make_command_update,
    seed_user,
    total_coins,
    user_state,
)
from game_support import (
    make_game_bot,
    make_game_context,
    make_job_context,
    set_permission,
)
from mysql_support import execute, fetch, fetch_scalar, run

from fogmoe_telegram_bot.core import balance
from fogmoe_telegram_bot.features.games import gamble, gamble_rounds

CHAT = -100
PANEL = 50


@pytest.fixture(autouse=True)
def _fresh_panel_locks(monkeypatch):
    """面板锁按轮次 id 缓存在进程里；每个测试的数据库都从 1 号轮次开始，也各用自己的事件循环。"""
    monkeypatch.setattr(gamble, "_panel_locks", {})


def new_round(url, *, chat_id=CHAT, message_id=PANEL):
    """开一局并登记面板消息，返回轮次 id。"""
    opened = run(gamble_rounds.open_round(chat_id))
    assert opened is not None
    assert run(gamble_rounds.attach_message(opened.id, message_id))
    return opened.id


def round_row(url, round_id):
    return fetch(url, "SELECT * FROM gamble_rounds WHERE id = %s", (round_id,))[0]


def bet_rows(url, round_id):
    return fetch(url, "SELECT * FROM gamble_bets WHERE round_id = %s ORDER BY id", (round_id,))


def make_due(url, round_id):
    execute(
        url,
        (
            "UPDATE gamble_rounds SET closes_at = UTC_TIMESTAMP(6) - INTERVAL 1 SECOND WHERE id = %s",
            (round_id,),
        ),
    )


def bet_click(user_id, data, *, chat_id=CHAT, message_id=PANEL):
    return make_callback_update(
        from_user_id=user_id, data=data, chat_id=chat_id, message_id=message_id
    )


def click(url, user_id, data, *, chat_id=CHAT, message_id=PANEL, context=None):
    update, answer, _ = bet_click(user_id, data, chat_id=chat_id, message_id=message_id)
    run(gamble.gamble_callback(update, context or make_game_context()))
    return answer


def bet(round_id, user_id, amount=5):
    return run(
        gamble_rounds.accept_bet(
            round_id,
            chat_id=CHAT,
            message_id=PANEL,
            user_id=user_id,
            username=f"user{user_id}",
            amount=amount,
        )
    )


class TestOpenRound:
    def test_only_one_round_can_be_open_at_a_time(self, app_database):
        first = run(gamble_rounds.open_round(CHAT))
        second = run(gamble_rounds.open_round(-200))

        assert first is not None and second is None
        assert fetch_scalar(app_database, "SELECT COUNT(*) FROM gamble_rounds") == 1

    def test_a_settled_round_frees_the_slot(self, app_database):
        round_id = new_round(app_database)
        run(gamble_rounds.settle_round(round_id))

        assert run(gamble_rounds.open_round(CHAT)) is not None
        assert round_row(app_database, round_id)["active_slot"] is None

    def test_a_round_without_a_panel_accepts_no_bets(self, app_database):
        seed_user(app_database, 1, free=20)
        opened = run(gamble_rounds.open_round(CHAT))

        with pytest.raises(gamble_rounds.BetRejected) as rejected:
            run(
                gamble_rounds.accept_bet(
                    opened.id,
                    chat_id=CHAT,
                    message_id=PANEL,
                    user_id=1,
                    username="u",
                    amount=5,
                )
            )

        assert rejected.value.code == gamble_rounds.CODE_STALE
        assert total_coins(app_database, 1) == 20

    def test_a_failed_panel_send_cancels_the_round_and_frees_the_slot(self, app_database):
        seed_user(app_database, 1)
        set_permission(app_database, 1)
        update = make_command_update(
            user_id=1, chat_id=CHAT, reply_text=Recorder(fail_times=1, error=RuntimeError("send"))
        )

        with pytest.raises(RuntimeError):
            run(gamble.gamble_command.__wrapped__(update, make_game_context()))

        assert fetch_scalar(app_database, "SELECT status FROM gamble_rounds") == "cancelled"
        assert run(gamble_rounds.open_round(CHAT)) is not None


class TestGambleCommand:
    def test_command_opens_a_round_with_buttons_bound_to_it_and_arms_the_timer(self, app_database):
        seed_user(app_database, 1)
        set_permission(app_database, 1)
        reply = Recorder(result=SimpleNamespace(message_id=PANEL))
        update = make_command_update(user_id=1, chat_id=CHAT, reply_text=reply)
        context = make_game_context()

        run(gamble.gamble_command.__wrapped__(update, context))

        row = fetch(app_database, "SELECT * FROM gamble_rounds")[0]
        assert row["status"] == "open" and row["chat_id"] == CHAT and row["message_id"] == PANEL
        markup = reply.calls[0][1]["reply_markup"]
        assert [b.callback_data for b in markup.inline_keyboard[0]] == [
            f"gamble_{row['id']}_5",
            f"gamble_{row['id']}_10",
            f"gamble_{row['id']}_20",
        ]
        timer = context.job_queue.jobs[0]
        assert timer.data == row["id"] and 300 <= timer.when <= 301

    def test_a_second_command_while_a_round_is_open_is_refused(self, app_database):
        seed_user(app_database, 1)
        set_permission(app_database, 1)
        new_round(app_database)
        update = make_command_update(user_id=1, chat_id=CHAT)

        run(gamble.gamble_command.__wrapped__(update, make_game_context()))

        assert "进行中" in update.message.reply_text.texts[-1]
        assert fetch_scalar(app_database, "SELECT COUNT(*) FROM gamble_rounds") == 1

    def test_users_without_permission_cannot_start_a_round(self, app_database):
        seed_user(app_database, 1)
        update = make_command_update(user_id=1, chat_id=CHAT)

        run(gamble.gamble_command.__wrapped__(update, make_game_context()))

        assert "权限不足" in update.message.reply_text.texts[-1]
        assert fetch_scalar(app_database, "SELECT COUNT(*) FROM gamble_rounds") == 0

    def test_a_new_command_settles_an_overdue_round_first(self, app_database):
        seed_user(app_database, 1, free=10)
        seed_user(app_database, 2)
        set_permission(app_database, 2)
        old = new_round(app_database)
        bet(old, 1)
        make_due(app_database, old)
        update = make_command_update(user_id=2, chat_id=CHAT, reply_text=Recorder(result=SimpleNamespace(message_id=77)))

        run(gamble.gamble_command.__wrapped__(update, make_game_context()))

        assert round_row(app_database, old)["status"] == "settled"
        assert total_coins(app_database, 1) == 10  # 唯一的参与者拿回奖池
        assert fetch_scalar(app_database, "SELECT COUNT(*) FROM gamble_rounds WHERE status='open'") == 1


class TestBetAcceptance:
    def test_a_bet_is_debited_and_registered_together(self, app_database):
        seed_user(app_database, 1, free=3, paid=10)
        round_id = new_round(app_database)

        accepted = bet(round_id, 1, 10)

        assert accepted.op_key == f"gamble:{round_id}:bet:1"
        assert user_state(app_database, 1)["free"] == 0
        assert total_coins(app_database, 1) == 3
        assert [(r["user_id"], r["amount"], r["op_key"]) for r in bet_rows(app_database, round_id)] == [
            (1, 10, f"gamble:{round_id}:bet:1")
        ]
        assert [(r["op_key"], r["kind"], r["reason"]) for r in ledger_rows(app_database, 1)] == [
            (f"gamble:{round_id}:bet:1", "debit", "gamble_bet")
        ]

    def test_two_concurrent_bets_from_one_player_are_debited_and_registered_once(
        self, app_database
    ):
        seed_user(app_database, 1, free=20)
        round_id = new_round(app_database)

        async def scenario():
            return await gather_all(
                *(
                    gamble_rounds.accept_bet(
                        round_id,
                        chat_id=CHAT,
                        message_id=PANEL,
                        user_id=1,
                        username="user1",
                        amount=5,
                    )
                    for _ in range(3)
                )
            )

        results = run(scenario())

        accepted = [r for r in results if isinstance(r, gamble_rounds.Bet)]
        rejected = [r for r in results if isinstance(r, gamble_rounds.BetRejected)]
        assert len(accepted) == 1 and len(rejected) == 2, results
        assert {r.code for r in rejected} == {gamble_rounds.CODE_ALREADY_BET}
        assert total_coins(app_database, 1) == 15
        assert len(bet_rows(app_database, round_id)) == 1
        assert len(ledger_rows(app_database, 1)) == 1

    def test_concurrent_clicks_through_the_handler_charge_once(self, app_database):
        seed_user(app_database, 1, free=20)
        round_id = new_round(app_database)
        context = make_game_context(edit=Recorder())
        clicks = [bet_click(1, f"gamble_{round_id}_5") for _ in range(2)]

        async def scenario():
            await gather_all(*(gamble.gamble_callback(update, context) for update, _, _ in clicks))

        run(scenario())

        assert total_coins(app_database, 1) == 15
        answers = sorted(text for _, answer, _ in clicks for text in answer.texts)
        assert any("成功押注 5" in text for text in answers)
        assert any("已参与" in text for text in answers)

    def test_insufficient_balance_rejects_the_bet_without_registering_it(self, app_database):
        seed_user(app_database, 1, free=4)
        round_id = new_round(app_database)

        answer = click(app_database, 1, f"gamble_{round_id}_5")

        assert "硬币不足" in answer.texts[-1]
        assert total_coins(app_database, 1) == 4
        assert bet_rows(app_database, round_id) == []
        assert ledger_rows(app_database) == []

    def test_a_debit_failure_rolls_back_the_registered_bet(self, app_database, monkeypatch):
        seed_user(app_database, 1, free=20)
        round_id = new_round(app_database)

        async def broken_debit(*args, **kwargs):
            raise RuntimeError("injected debit failure")

        monkeypatch.setattr(balance, "debit", broken_debit)
        answer = click(app_database, 1, f"gamble_{round_id}_5")

        assert "出错" in answer.texts[-1]
        assert total_coins(app_database, 1) == 20
        assert bet_rows(app_database, round_id) == []

    def test_unregistered_users_are_told_to_register(self, app_database):
        round_id = new_round(app_database)

        answer = click(app_database, 99, f"gamble_{round_id}_5")

        assert "/me" in answer.texts[-1]
        assert bet_rows(app_database, round_id) == []

    def test_a_successful_click_refreshes_the_panel_with_the_participants(self, app_database):
        seed_user(app_database, 1, free=20)
        round_id = new_round(app_database)
        context = make_game_context(edit=Recorder())

        click(app_database, 1, f"gamble_{round_id}_10", context=context)

        edit = context.bot.edit_message_text
        assert len(edit.calls) == 1
        assert edit.calls[0][1]["chat_id"] == CHAT and edit.calls[0][1]["message_id"] == PANEL
        assert "@user1 押注 10 金币" in edit.texts[0]


class TestManyPlayers:
    def test_players_betting_at_the_same_time_are_all_accepted(self, app_database):
        for user_id in range(1, 7):
            seed_user(app_database, user_id, free=20)
        round_id = new_round(app_database)

        async def scenario():
            return await gather_all(
                *(
                    gamble_rounds.accept_bet(
                        round_id,
                        chat_id=CHAT,
                        message_id=PANEL,
                        user_id=user_id,
                        username=f"user{user_id}",
                        amount=5,
                    )
                    for user_id in range(1, 7)
                )
            )

        results = run(scenario())

        assert all(isinstance(r, gamble_rounds.Bet) for r in results), results
        assert len(bet_rows(app_database, round_id)) == 6
        assert sum(total_coins(app_database, user_id) for user_id in range(1, 7)) == 120 - 30

    def test_bets_racing_with_the_settlement_are_either_counted_or_refused_never_lost(
        self, app_database
    ):
        for user_id in range(1, 7):
            seed_user(app_database, user_id, free=20)
        round_id = new_round(app_database)

        async def scenario():
            bets = [
                gamble_rounds.accept_bet(
                    round_id,
                    chat_id=CHAT,
                    message_id=PANEL,
                    user_id=user_id,
                    username=f"user{user_id}",
                    amount=5,
                )
                for user_id in range(1, 7)
            ]
            return await gather_all(gamble_rounds.settle_round(round_id), *bets)

        results = run(scenario())

        settlement, outcomes = results[0], results[1:]
        assert isinstance(settlement, gamble_rounds.Settlement), results
        assert all(
            isinstance(r, gamble_rounds.Bet)
            or (isinstance(r, gamble_rounds.BetRejected) and r.code == gamble_rounds.CODE_CLOSED)
            for r in outcomes
        ), outcomes
        accepted = [r for r in outcomes if isinstance(r, gamble_rounds.Bet)]
        # 结算时统计到的下注，就是最终留在表里的全部下注；奖池整个归了中奖者，金币总量不变。
        assert len(bet_rows(app_database, round_id)) == len(accepted) == len(settlement.bets)
        assert settlement.round.prize == 5 * len(accepted)
        assert sum(total_coins(app_database, user_id) for user_id in range(1, 7)) == 120


class TestStalePanels:
    @pytest.mark.parametrize("data", ["gamble_5", "gamble_x_5", "gamble_1_2_3", "gamble_1_7", "gamble"])
    def test_old_or_malformed_callbacks_are_rejected_without_a_charge(self, app_database, data):
        seed_user(app_database, 1, free=20)
        round_id = new_round(app_database)

        answer = click(app_database, 1, data.replace("gamble_1", f"gamble_{round_id}"))

        assert "失效" in answer.texts[-1]
        assert total_coins(app_database, 1) == 20
        assert bet_rows(app_database, round_id) == []

    def test_an_old_round_id_is_rejected_even_when_the_panel_position_matches(self, app_database):
        seed_user(app_database, 1, free=20)
        old = new_round(app_database)
        run(gamble_rounds.settle_round(old))
        current = new_round(app_database)  # 同一个 chat、同一个面板位置的新一局
        assert current != old

        answer = click(app_database, 1, f"gamble_{old}_5")

        assert "已停止接受押注" in answer.texts[-1]
        assert total_coins(app_database, 1) == 20
        assert bet_rows(app_database, old) == [] and bet_rows(app_database, current) == []

    def test_an_unknown_round_id_is_rejected(self, app_database):
        seed_user(app_database, 1, free=20)
        new_round(app_database)

        answer = click(app_database, 1, "gamble_999_5")

        assert "失效" in answer.texts[-1]
        assert total_coins(app_database, 1) == 20

    def test_a_panel_from_another_chat_cannot_bet_into_the_current_round(self, app_database):
        seed_user(app_database, 1, free=20)
        round_id = new_round(app_database)

        answer = click(app_database, 1, f"gamble_{round_id}_5", chat_id=-777)

        assert "失效" in answer.texts[-1]
        assert total_coins(app_database, 1) == 20
        assert bet_rows(app_database, round_id) == []

    def test_another_message_in_the_same_chat_cannot_bet_either(self, app_database):
        seed_user(app_database, 1, free=20)
        round_id = new_round(app_database)

        answer = click(app_database, 1, f"gamble_{round_id}_5", message_id=PANEL + 1)

        assert "失效" in answer.texts[-1]
        assert total_coins(app_database, 1) == 20

    def test_a_round_past_its_deadline_rejects_bets_before_it_is_settled(self, app_database):
        seed_user(app_database, 1, free=20)
        round_id = new_round(app_database)
        make_due(app_database, round_id)

        answer = click(app_database, 1, f"gamble_{round_id}_5")

        assert "已停止接受押注" in answer.texts[-1]
        assert total_coins(app_database, 1) == 20
        assert round_row(app_database, round_id)["status"] == "open"


class TestRoundClosingDuringABet:
    def test_a_bet_that_loses_the_race_to_settlement_is_rejected_without_a_charge(
        self, app_database, monkeypatch
    ):
        seed_user(app_database, 1, free=20)
        seed_user(app_database, 2, free=20)
        round_id = new_round(app_database)
        bet(round_id, 1)
        real_credit = balance.credit
        entered, release = None, None

        async def scenario():
            nonlocal entered, release
            entered, release = asyncio.Event(), asyncio.Event()

            async def paused_credit(*args, **kwargs):
                entered.set()  # 结算已经拿到轮次行锁，正在入账
                await release.wait()
                return await real_credit(*args, **kwargs)

            monkeypatch.setattr(balance, "credit", paused_credit)
            update, answer, _ = bet_click(2, f"gamble_{round_id}_5")
            settle_task = asyncio.create_task(gamble_rounds.settle_round(round_id))
            await entered.wait()
            bet_task = asyncio.create_task(gamble.gamble_callback(update, make_game_context()))
            await asyncio.sleep(0.5)
            assert not bet_task.done()  # 下注卡在轮次行锁上，没有抢先扣款
            release.set()
            await settle_task
            await bet_task  # 不能抛 TypeError 之类的异常
            return answer

        answer = run(scenario())

        assert "已停止接受押注" in answer.texts[-1]
        assert total_coins(app_database, 2) == 20
        assert [r["user_id"] for r in bet_rows(app_database, round_id)] == [1]
        assert ledger_rows(app_database, 2) == []
        assert total_coins(app_database, 1) == 20 - 5 + 5  # 唯一的下注者赢回自己的奖池


class TestSettlement:
    def test_the_winner_is_paid_the_whole_pool_under_the_payout_op_key(self, app_database):
        seed_user(app_database, 1, free=20)
        seed_user(app_database, 2, free=20)
        round_id = new_round(app_database)
        bet(round_id, 1, 5)
        bet(round_id, 2, 20)

        settlement = run(gamble_rounds.settle_round(round_id))

        assert settlement.transitioned and settlement.winner is not None
        winner_id = settlement.winner.user_id
        loser_id = 1 if winner_id == 2 else 2
        assert settlement.round.prize == 25
        assert total_coins(app_database, winner_id) == 20 - settlement.winner.amount + 25
        assert total_coins(app_database, loser_id) == 20 - (5 if loser_id == 1 else 20)
        row = round_row(app_database, round_id)
        assert row["status"] == "settled" and row["winner_id"] == winner_id and row["prize"] == 25
        assert row["active_slot"] is None
        payouts = [r for r in ledger_rows(app_database) if r["reason"] == "gamble_win"]
        assert [(r["op_key"], r["user_id"], r["delta_free"]) for r in payouts] == [
            (f"gamble:{round_id}:payout", winner_id, 25)
        ]

    def test_concurrent_settlements_transition_once_and_pay_once(self, app_database):
        seed_user(app_database, 1, free=20)
        seed_user(app_database, 2, free=20)
        round_id = new_round(app_database)
        bet(round_id, 1, 5)
        bet(round_id, 2, 10)

        async def scenario():
            return await gather_all(*(gamble_rounds.settle_round(round_id) for _ in range(4)))

        results = run(scenario())

        assert all(isinstance(r, gamble_rounds.Settlement) for r in results), results
        assert sorted(r.transitioned for r in results) == [False, False, False, True]
        assert len({r.winner.user_id for r in results}) == 1  # 每个调用方看到的是同一个中奖者
        payouts = [r for r in ledger_rows(app_database) if r["reason"] == "gamble_win"]
        assert len(payouts) == 1
        assert total_coins(app_database, 1) + total_coins(app_database, 2) == 40 - 15 + 15

    def test_settling_a_round_with_no_bets_just_closes_it(self, app_database):
        round_id = new_round(app_database)

        settlement = run(gamble_rounds.settle_round(round_id))

        assert settlement.transitioned and settlement.winner is None and settlement.round.prize == 0
        assert round_row(app_database, round_id)["status"] == "settled"
        assert ledger_rows(app_database) == []

    def test_settling_an_unknown_round_does_nothing(self, app_database):
        assert run(gamble_rounds.settle_round(12345)) is None

    def test_a_round_that_is_not_due_is_left_alone_by_a_due_only_settle(self, app_database):
        seed_user(app_database, 1, free=20)
        round_id = new_round(app_database)
        bet(round_id, 1)

        settlement = run(gamble_rounds.settle_round(round_id, only_if_due=True))

        assert not settlement.transitioned
        assert round_row(app_database, round_id)["status"] == "open"
        assert total_coins(app_database, 1) == 15

    def test_a_payout_failure_rolls_the_settlement_back_and_a_retry_pays(
        self, app_database, monkeypatch
    ):
        seed_user(app_database, 1, free=20)
        round_id = new_round(app_database)
        bet(round_id, 1)
        real_credit = balance.credit
        attempts = []

        async def flaky_credit(*args, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("injected payout failure")
            return await real_credit(*args, **kwargs)

        monkeypatch.setattr(balance, "credit", flaky_credit)
        with pytest.raises(RuntimeError):
            run(gamble_rounds.settle_round(round_id))
        assert round_row(app_database, round_id)["status"] == "open"
        assert total_coins(app_database, 1) == 15

        settlement = run(gamble_rounds.settle_round(round_id))

        assert settlement.transitioned
        assert total_coins(app_database, 1) == 20
        assert len([r for r in ledger_rows(app_database) if r["reason"] == "gamble_win"]) == 1

    def test_a_missing_winner_account_turns_the_settlement_into_a_refund(
        self, app_database, monkeypatch
    ):
        seed_user(app_database, 1, free=20)
        seed_user(app_database, 2, free=20)
        round_id = new_round(app_database)
        first = bet(round_id, 1)
        bet(round_id, 2)
        execute(app_database, ("DELETE FROM `user` WHERE id = %s", (1,)))
        monkeypatch.setattr(gamble_rounds, "draw_winner", lambda bets, rng=None: first)

        settlement = run(gamble_rounds.settle_round(round_id))

        assert settlement.transitioned and settlement.round.status == "refunded"
        assert settlement.winner is None
        assert total_coins(app_database, 2) == 20
        assert round_row(app_database, round_id)["active_slot"] is None
        assert [r["kind"] for r in ledger_rows(app_database, 2)] == ["debit", "refund"]


class TestRecovery:
    def test_overdue_rounds_are_settled_and_announced_after_a_restart(self, app_database):
        seed_user(app_database, 1, free=20)
        round_id = new_round(app_database)
        bet(round_id, 1, 10)
        make_due(app_database, round_id)
        bot = make_game_bot(edit=Recorder())

        run(gamble.recover_gamble_rounds(make_job_context(bot)))

        row = round_row(app_database, round_id)
        assert row["status"] == "settled" and row["winner_id"] == 1 and row["announced_at"] is not None
        assert total_coins(app_database, 1) == 20
        assert "中奖者：@user1" in bot.edit_message_text.texts[-1]
        assert bot.edit_message_text.calls[-1][1]["message_id"] == PANEL

    def test_recovery_is_idempotent(self, app_database):
        seed_user(app_database, 1, free=20)
        seed_user(app_database, 2, free=20)
        round_id = new_round(app_database)
        bet(round_id, 1)
        bet(round_id, 2)
        make_due(app_database, round_id)
        bot = make_game_bot(edit=Recorder())
        context = make_job_context(bot)

        run(gamble.recover_gamble_rounds(context))
        ledger_after_first = ledger_rows(app_database)
        run(gamble.recover_gamble_rounds(context))
        run(gamble.recover_gamble_rounds(context))

        assert ledger_rows(app_database) == ledger_after_first
        assert len(bot.edit_message_text.calls) == 1
        assert total_coins(app_database, 1) + total_coins(app_database, 2) == 40

    def test_a_round_that_is_not_due_stays_open_and_keeps_accepting_bets(self, app_database):
        seed_user(app_database, 1, free=20)
        seed_user(app_database, 2, free=20)
        round_id = new_round(app_database)
        bet(round_id, 1)
        bot = make_game_bot(edit=Recorder())

        run(gamble.recover_gamble_rounds(make_job_context(bot)))
        late = bet(round_id, 2)

        assert round_row(app_database, round_id)["status"] == "open"
        assert late.user_id == 2
        assert bot.edit_message_text.calls == []

    def test_the_exact_timer_settles_the_round_once_it_is_due(self, app_database):
        seed_user(app_database, 1, free=20)
        round_id = new_round(app_database)
        bet(round_id, 1)
        bot = make_game_bot(edit=Recorder())

        run(gamble.settle_round_job(make_job_context(bot, round_id)))
        assert round_row(app_database, round_id)["status"] == "open"  # 还没到截止时间
        make_due(app_database, round_id)
        run(gamble.settle_round_job(make_job_context(bot, round_id)))
        run(gamble.settle_round_job(make_job_context(bot, round_id)))

        assert round_row(app_database, round_id)["status"] == "settled"
        assert len([r for r in ledger_rows(app_database) if r["reason"] == "gamble_win"]) == 1
        assert len(bot.edit_message_text.calls) == 1

    def test_a_failed_announcement_is_retried_without_settling_again(self, app_database):
        seed_user(app_database, 1, free=20)
        round_id = new_round(app_database)
        bet(round_id, 1)
        make_due(app_database, round_id)
        flaky = Recorder(fail_times=1, error=RuntimeError("network down"))
        bot = make_game_bot(edit=flaky)

        run(gamble.recover_gamble_rounds(make_job_context(bot)))
        assert round_row(app_database, round_id)["announced_at"] is None
        ledger_after_settle = ledger_rows(app_database)
        run(gamble.recover_gamble_rounds(make_job_context(bot)))

        assert round_row(app_database, round_id)["announced_at"] is not None
        assert ledger_rows(app_database) == ledger_after_settle
        assert len(flaky.calls) == 2

    def test_a_panel_telegram_refuses_to_edit_is_not_retried_forever(self, app_database):
        from telegram.error import BadRequest

        round_id = new_round(app_database)
        make_due(app_database, round_id)
        bot = make_game_bot(edit=Recorder(error=BadRequest("message to edit not found"), fail_times=99))

        run(gamble.recover_gamble_rounds(make_job_context(bot)))
        run(gamble.recover_gamble_rounds(make_job_context(bot)))

        assert round_row(app_database, round_id)["announced_at"] is not None
        assert len(bot.edit_message_text.calls) == 1

    def test_a_round_that_never_got_a_panel_is_closed_without_an_edit(self, app_database):
        opened = run(gamble_rounds.open_round(CHAT))
        make_due(app_database, opened.id)
        bot = make_game_bot(edit=Recorder())

        run(gamble.recover_gamble_rounds(make_job_context(bot)))

        row = round_row(app_database, opened.id)
        assert row["status"] == "settled" and row["announced_at"] is not None
        assert bot.edit_message_text.calls == []
        assert run(gamble_rounds.open_round(CHAT)) is not None

    def test_an_unannounced_old_round_is_not_retried_forever(self, app_database):
        round_id = new_round(app_database)
        run(gamble_rounds.settle_round(round_id))
        execute(
            app_database,
            (
                "UPDATE gamble_rounds SET settled_at = UTC_TIMESTAMP(6) - INTERVAL 3 DAY WHERE id = %s",
                (round_id,),
            ),
        )

        assert run(gamble_rounds.unannounced_round_ids()) == []


def test_weighted_draw_follows_the_bet_amounts():
    class FirstWins:
        def choices(self, population, weights, k):
            self.weights = list(weights)
            return [population[0]]

    bets = (
        gamble_rounds.Bet(1, "a", 5, "k1"),
        gamble_rounds.Bet(2, "b", 20, "k2"),
    )
    rng = FirstWins()

    assert gamble_rounds.draw_winner(bets, rng).user_id == 1
    assert rng.weights == [5, 20]
