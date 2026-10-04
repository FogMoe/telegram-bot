"""石头剪刀布：入场扣款与建局同事务、退款与奖金只发生一次、超时与重启恢复（真实 MySQL）。"""

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
)
from game_support import MessageSender, make_game_bot, make_game_context, make_job_context
from mysql_support import execute, fetch, fetch_scalar, run

from core import balance
from features.games import rockpaperscissors_game as rps
from features.games import rps_games
from features.games.rps_games import Seat

CHAT = -100
WAITING_MESSAGE = 500


@pytest.fixture(autouse=True)
def _fresh_waiting_room(monkeypatch):
    """等待房间与它的锁是进程内状态；每个测试各用一份，锁也不能跨事件循环复用。"""
    monkeypatch.setattr(rps, "waiting_room", None)
    monkeypatch.setattr(rps, "waiting_room_lock", asyncio.Lock())


def game_row(url, game_id):
    return fetch(url, "SELECT * FROM rps_games WHERE id = %s", (game_id,))[0]


def make_expired(url, game_id):
    execute(
        url,
        (
            "UPDATE rps_games SET expires_at = UTC_TIMESTAMP(6) - INTERVAL 1 SECOND WHERE id = %s",
            (game_id,),
        ),
    )


def seats(*, same_chat=True, p1=1, p2=2):
    return (
        Seat(p1, f"user{p1}", CHAT, WAITING_MESSAGE),
        Seat(p2, f"user{p2}", CHAT if same_chat else -200, WAITING_MESSAGE if same_chat else None),
    )


def start(url, *, same_chat=True, p1=1, p2=2):
    first, second = seats(same_chat=same_chat, p1=p1, p2=p2)
    return run(rps_games.create_game(first, second, same_chat=same_chat))


def entry_keys(url, user_id=None):
    return [r["op_key"] for r in ledger_rows(url, user_id)]


@pytest.fixture
def players(app_database):
    seed_user(app_database, 1, free=5)
    seed_user(app_database, 2, free=5)
    return app_database


class TestCreateGame:
    def test_both_entries_are_debited_with_the_game_creation(self, players):
        game = start(players)

        assert game.status == "choosing" and game.p1.user_id == 1 and game.p2.user_id == 2
        assert total_coins(players, 1) == 4 and total_coins(players, 2) == 4
        assert sorted(entry_keys(players)) == [
            f"rps:{game.id}:entry:1",
            f"rps:{game.id}:entry:2",
        ]
        row = game_row(players, game.id)
        assert row["p1_message_id"] == WAITING_MESSAGE and row["same_chat"] == 1
        assert 118 <= game.seconds_left <= 120

    def test_a_joiner_who_cannot_pay_prevents_the_game_and_spares_the_waiting_player(
        self, app_database
    ):
        seed_user(app_database, 1, free=5)
        seed_user(app_database, 2, free=0)

        with pytest.raises(rps_games.StartRejected) as rejected:
            start(app_database)

        assert rejected.value.code == rps_games.CODE_INSUFFICIENT and rejected.value.user_id == 2
        assert fetch_scalar(app_database, "SELECT COUNT(*) FROM rps_games") == 0
        assert total_coins(app_database, 1) == 5
        assert ledger_rows(app_database) == []

    def test_a_waiting_player_who_cannot_pay_prevents_the_game_and_spares_the_joiner(
        self, app_database
    ):
        seed_user(app_database, 1, free=0)
        seed_user(app_database, 2, free=5)

        with pytest.raises(rps_games.StartRejected) as rejected:
            start(app_database)

        assert rejected.value.code == rps_games.CODE_INSUFFICIENT and rejected.value.user_id == 1
        assert fetch_scalar(app_database, "SELECT COUNT(*) FROM rps_games") == 0
        assert total_coins(app_database, 2) == 5
        assert ledger_rows(app_database) == []

    def test_an_unregistered_player_prevents_the_game(self, app_database):
        seed_user(app_database, 1, free=5)

        with pytest.raises(rps_games.StartRejected) as rejected:
            start(app_database)

        assert rejected.value.code == rps_games.CODE_NO_USER and rejected.value.user_id == 2
        assert total_coins(app_database, 1) == 5
        assert fetch_scalar(app_database, "SELECT COUNT(*) FROM rps_games") == 0

    def test_an_unexpected_failure_on_the_second_debit_rolls_everything_back(
        self, players, monkeypatch
    ):
        real_debit = balance.debit
        calls = []

        async def second_debit_fails(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("injected failure")
            return await real_debit(*args, **kwargs)

        monkeypatch.setattr(balance, "debit", second_debit_fails)

        with pytest.raises(RuntimeError):
            start(players)

        assert fetch_scalar(players, "SELECT COUNT(*) FROM rps_games") == 0
        assert total_coins(players, 1) == 5 and total_coins(players, 2) == 5
        assert ledger_rows(players) == []

    def test_a_player_already_in_a_game_cannot_start_another(self, players):
        seed_user(players, 3, free=5)
        start(players)

        with pytest.raises(rps_games.StartRejected) as rejected:
            start(players, p1=1, p2=3)

        assert rejected.value.code == rps_games.CODE_BUSY and rejected.value.user_id == 1
        assert total_coins(players, 3) == 5
        assert fetch_scalar(players, "SELECT COUNT(*) FROM rps_games") == 1

    def test_concurrent_joiners_create_one_game_and_debit_the_waiting_player_once(self, players):
        seed_user(players, 3, free=5)

        async def scenario():
            return await gather_all(
                rps_games.create_game(*seats(p2=2), same_chat=True),
                rps_games.create_game(*seats(p2=3), same_chat=True),
            )

        results = run(scenario())

        created = [r for r in results if isinstance(r, rps_games.Game)]
        rejected = [r for r in results if isinstance(r, rps_games.StartRejected)]
        assert len(created) == 1 and len(rejected) == 1, results
        assert rejected[0].code == rps_games.CODE_BUSY
        assert total_coins(players, 1) == 4
        assert total_coins(players, 2) + total_coins(players, 3) == 9
        assert len(ledger_rows(players)) == 2

    def test_active_game_lookup_finds_either_player(self, players):
        game = start(players)

        assert run(rps_games.active_game_for(1)).id == game.id
        assert run(rps_games.active_game_for(2)).id == game.id
        assert run(rps_games.active_game_for(3)) is None


class TestChoicesAndSettlement:
    def choose(self, game_id, user_id, choice):
        return run(rps_games.record_choice(game_id, user_id, choice))

    def test_the_winner_is_paid_two_coins_under_a_game_derived_op_key(self, players):
        game = start(players)

        first = self.choose(game.id, 1, "rock")
        second = self.choose(game.id, 2, "scissors")

        assert first.code == rps_games.CHOICE_RECORDED
        assert second.code == rps_games.CHOICE_SETTLED
        assert second.game.status == "settled" and second.game.outcome == "p1"
        assert total_coins(players, 1) == 4 + 2 and total_coins(players, 2) == 4
        payout = [r for r in ledger_rows(players) if r["reason"] == "rps_win"]
        assert [(r["op_key"], r["user_id"]) for r in payout] == [(f"rps:{game.id}:win", 1)]

    @pytest.mark.parametrize(
        ("choice1", "choice2", "outcome"),
        [
            ("rock", "scissors", "p1"),
            ("scissors", "paper", "p1"),
            ("paper", "rock", "p1"),
            ("scissors", "rock", "p2"),
            ("paper", "scissors", "p2"),
            ("rock", "paper", "p2"),
            ("rock", "rock", "draw"),
            ("paper", "paper", "draw"),
            ("scissors", "scissors", "draw"),
        ],
    )
    def test_every_pairing_has_the_expected_outcome(self, choice1, choice2, outcome):
        assert rps_games.decide(choice1, choice2) == outcome

    def test_a_draw_refunds_both_entries_once(self, players):
        game = start(players)

        self.choose(game.id, 1, "paper")
        result = self.choose(game.id, 2, "paper")

        assert result.game.outcome == "draw"
        assert total_coins(players, 1) == 5 and total_coins(players, 2) == 5
        assert sorted(entry_keys(players)) == sorted(
            [
                f"rps:{game.id}:entry:1",
                f"rps:{game.id}:entry:2",
                f"refund:rps:{game.id}:entry:1",
                f"refund:rps:{game.id}:entry:2",
            ]
        )

    def test_a_player_cannot_choose_twice_and_a_stranger_cannot_choose(self, players):
        seed_user(players, 3, free=5)
        game = start(players)
        self.choose(game.id, 1, "rock")

        again = self.choose(game.id, 1, "paper")
        stranger = self.choose(game.id, 3, "rock")
        unknown = self.choose(9999, 1, "rock")

        assert again.code == rps_games.CHOICE_ALREADY
        assert stranger.code == rps_games.CHOICE_NOT_PLAYER
        assert unknown.code == rps_games.CHOICE_NOT_FOUND
        assert game_row(players, game.id)["p1_choice"] == "rock"

    def test_choices_after_the_game_has_finished_change_nothing(self, players):
        game = start(players)
        self.choose(game.id, 1, "rock")
        self.choose(game.id, 2, "scissors")

        late = self.choose(game.id, 1, "paper")

        assert late.code == rps_games.CHOICE_FINISHED
        assert total_coins(players, 1) == 6 and total_coins(players, 2) == 4
        assert len(ledger_rows(players)) == 3

    def test_both_players_choosing_at_the_same_moment_settle_once(self, players):
        game = start(players)

        async def scenario():
            return await gather_all(
                rps_games.record_choice(game.id, 1, "rock"),
                rps_games.record_choice(game.id, 2, "scissors"),
            )

        results = run(scenario())

        assert all(isinstance(r, rps_games.ChoiceResult) for r in results), results
        assert sorted(r.code for r in results) == [
            rps_games.CHOICE_RECORDED,
            rps_games.CHOICE_SETTLED,
        ]
        assert len([r for r in ledger_rows(players) if r["reason"] == "rps_win"]) == 1
        assert total_coins(players, 1) + total_coins(players, 2) == 10

    def test_a_failed_payout_keeps_the_game_open_and_nothing_is_paid(self, players, monkeypatch):
        game = start(players)
        self.choose(game.id, 1, "rock")
        real_credit = balance.credit
        calls = []

        async def flaky_credit(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("injected payout failure")
            return await real_credit(*args, **kwargs)

        monkeypatch.setattr(balance, "credit", flaky_credit)
        with pytest.raises(RuntimeError):
            self.choose(game.id, 2, "scissors")

        row = game_row(players, game.id)
        assert row["status"] == "choosing" and row["p2_choice"] is None
        assert total_coins(players, 1) == 4
        # 重试时选择重新提交，奖金只入账一次
        retry = self.choose(game.id, 2, "scissors")
        assert retry.code == rps_games.CHOICE_SETTLED
        assert total_coins(players, 1) == 6
        assert len([r for r in ledger_rows(players) if r["reason"] == "rps_win"]) == 1

    def test_a_winner_whose_account_is_gone_turns_the_game_into_a_refund(self, players):
        game = start(players)
        self.choose(game.id, 1, "rock")
        execute(players, ("DELETE FROM `user` WHERE id = %s", (1,)))

        result = self.choose(game.id, 2, "scissors")

        assert result.game.status == "refunded" and result.game.outcome == "failed"
        assert total_coins(players, 2) == 5


class TestTimeoutAndCancel:
    def test_an_expired_game_refunds_both_entries_once(self, players):
        game = start(players)
        make_expired(players, game.id)

        first = run(rps_games.expire_game(game.id))
        second = run(rps_games.expire_game(game.id))

        assert first is not None and first.status == "refunded" and first.outcome == "timeout"
        assert second is None
        assert total_coins(players, 1) == 5 and total_coins(players, 2) == 5
        assert len([r for r in ledger_rows(players) if r["kind"] == "refund"]) == 2

    def test_a_game_that_is_not_due_is_not_expired(self, players):
        game = start(players)

        assert run(rps_games.expire_game(game.id)) is None
        assert game_row(players, game.id)["status"] == "choosing"
        assert total_coins(players, 1) == 4

    def test_concurrent_expiries_refund_once(self, players):
        game = start(players)
        make_expired(players, game.id)

        async def scenario():
            return await gather_all(*(rps_games.expire_game(game.id) for _ in range(4)))

        results = run(scenario())

        assert all(not isinstance(r, Exception) for r in results), results
        assert len([r for r in results if r is not None]) == 1
        assert total_coins(players, 1) == 5 and total_coins(players, 2) == 5
        assert len(ledger_rows(players)) == 4

    def test_a_choice_after_the_deadline_refunds_instead_of_playing(self, players):
        game = start(players)
        run(rps_games.record_choice(game.id, 1, "rock"))
        make_expired(players, game.id)

        late = run(rps_games.record_choice(game.id, 2, "scissors"))

        assert late.code == rps_games.CHOICE_EXPIRED and late.game.outcome == "timeout"
        assert game_row(players, game.id)["p2_choice"] is None
        assert total_coins(players, 1) == 5 and total_coins(players, 2) == 5

    def test_cancel_refunds_both_entries_once_and_marks_the_game_failed(self, players):
        game = start(players)

        first = run(rps_games.cancel_game(game.id))
        second = run(rps_games.cancel_game(game.id))

        assert first.status == "refunded" and first.outcome == "failed" and second is None
        assert total_coins(players, 1) == 5 and total_coins(players, 2) == 5
        assert len([r for r in ledger_rows(players) if r["kind"] == "refund"]) == 2

    def test_a_refund_failure_leaves_the_game_open_for_a_retry(self, players, monkeypatch):
        game = start(players)
        make_expired(players, game.id)
        real_refund = balance.refund
        calls = []

        async def second_refund_fails(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("injected refund failure")
            return await real_refund(*args, **kwargs)

        monkeypatch.setattr(balance, "refund", second_refund_fails)
        with pytest.raises(RuntimeError):
            run(rps_games.expire_game(game.id))
        assert game_row(players, game.id)["status"] == "choosing"
        assert total_coins(players, 1) == 4 and total_coins(players, 2) == 4

        assert run(rps_games.expire_game(game.id)) is not None
        assert total_coins(players, 1) == 5 and total_coins(players, 2) == 5


class TestRecovery:
    def test_games_that_expired_while_the_process_was_down_are_refunded_and_announced(
        self, players
    ):
        game = start(players)
        run(rps_games.record_message_ids(game.id, p1_private_msg_id=11, p2_private_msg_id=12))
        make_expired(players, game.id)
        bot = make_game_bot(edit=Recorder())

        run(rps.recover_rps_games(make_job_context(bot)))

        row = game_row(players, game.id)
        assert row["status"] == "refunded" and row["outcome"] == "timeout"
        assert row["announced_at"] is not None
        assert total_coins(players, 1) == 5 and total_coins(players, 2) == 5
        edited = {(c[1]["chat_id"], c[1]["message_id"]) for c in bot.edit_message_text.calls}
        assert edited == {(CHAT, WAITING_MESSAGE), (1, 11), (2, 12)}
        assert "已超时" in bot.edit_message_text.texts[0]

    def test_recovery_is_idempotent(self, players):
        game = start(players)
        make_expired(players, game.id)
        bot = make_game_bot(edit=Recorder())
        context = make_job_context(bot)

        run(rps.recover_rps_games(context))
        ledger_after_first = ledger_rows(players)
        calls_after_first = len(bot.edit_message_text.calls)
        run(rps.recover_rps_games(context))
        run(rps.recover_rps_games(context))

        assert ledger_rows(players) == ledger_after_first
        assert len(bot.edit_message_text.calls) == calls_after_first
        assert total_coins(players, 1) == 5 and total_coins(players, 2) == 5

    def test_games_that_are_not_expired_survive_a_restart_and_can_still_be_played(self, players):
        game = start(players)
        bot = make_game_bot(edit=Recorder())

        run(rps.recover_rps_games(make_job_context(bot)))
        run(rps_games.record_choice(game.id, 1, "rock"))
        result = run(rps_games.record_choice(game.id, 2, "paper"))

        assert result.game.outcome == "p2"
        assert total_coins(players, 2) == 6
        assert bot.edit_message_text.calls == []

    def test_a_failed_announcement_is_retried_without_refunding_again(self, players):
        game = start(players)
        make_expired(players, game.id)
        flaky = Recorder(fail_times=1, error=RuntimeError("network down"))
        bot = make_game_bot(edit=flaky)

        run(rps.recover_rps_games(make_job_context(bot)))
        assert game_row(players, game.id)["announced_at"] is None
        ledger_after_refund = ledger_rows(players)
        run(rps.recover_rps_games(make_job_context(bot)))

        assert game_row(players, game.id)["announced_at"] is not None
        assert ledger_rows(players) == ledger_after_refund

    def test_the_exact_timer_refunds_a_game_once_it_is_due(self, players):
        game = start(players)
        bot = make_game_bot(edit=Recorder())

        run(rps.game_timeout_job(make_job_context(bot, game.id)))
        assert game_row(players, game.id)["status"] == "choosing"
        make_expired(players, game.id)
        run(rps.game_timeout_job(make_job_context(bot, game.id)))
        run(rps.game_timeout_job(make_job_context(bot, game.id)))

        assert game_row(players, game.id)["status"] == "refunded"
        assert total_coins(players, 1) == 5 and total_coins(players, 2) == 5
        assert len([r for r in ledger_rows(players) if r["kind"] == "refund"]) == 2


def command(user_id, *, chat_id=CHAT, reply_id=None):
    reply = Recorder(result=SimpleNamespace(message_id=reply_id or WAITING_MESSAGE))
    return make_command_update(user_id=user_id, chat_id=chat_id, reply_text=reply)


def join_click(user_id, *, chat_id=CHAT, message_id=WAITING_MESSAGE):
    return make_callback_update(
        from_user_id=user_id, data="rps_join", chat_id=chat_id, message_id=message_id
    )


class TestHandlers:
    def open_room(self):
        update = command(1)
        context = make_game_context()
        run(rps.rps_game_command.__wrapped__(update, context))
        return update, context

    def test_the_first_player_opens_a_waiting_room_that_holds_no_coins(self, players):
        update, context = self.open_room()

        assert rps.waiting_room["player_id"] == 1 and rps.waiting_room["message_id"] == WAITING_MESSAGE
        assert total_coins(players, 1) == 5
        assert fetch_scalar(players, "SELECT COUNT(*) FROM rps_games") == 0
        assert context.job_queue.jobs[0].data == (1, WAITING_MESSAGE)

    def test_a_joiner_in_the_same_chat_starts_a_game_with_buttons_bound_to_its_id(self, players):
        self.open_room()
        send = MessageSender(first_id=700)
        context = make_game_context(send=send)

        run(rps.rps_game_command.__wrapped__(command(2), context))

        game = fetch(players, "SELECT * FROM rps_games")[0]
        assert rps.waiting_room is None
        assert total_coins(players, 1) == 4 and total_coins(players, 2) == 4
        assert game["p1_private_msg_id"] == 700 and game["p2_private_msg_id"] == 701
        assert [c["chat_id"] for c in send.calls] == [1, 2]
        buttons = [b.callback_data for b in send.calls[0]["reply_markup"].inline_keyboard[0]]
        assert buttons == [
            f"rps_choice_{game['id']}_rock_1",
            f"rps_choice_{game['id']}_scissors_1",
            f"rps_choice_{game['id']}_paper_1",
        ]
        timer = context.job_queue.jobs[0]
        assert timer.data == game["id"] and 120 <= timer.when <= 121
        assert context.bot.edit_message_text.calls[0][1]["message_id"] == WAITING_MESSAGE

    def test_a_joiner_in_another_chat_gets_a_panel_in_their_own_chat(self, players):
        self.open_room()
        context = make_game_context()
        joiner = command(2, chat_id=-200, reply_id=900)

        run(rps.rps_game_command.__wrapped__(joiner, context))

        game = fetch(players, "SELECT * FROM rps_games")[0]
        assert game["same_chat"] == 0 and game["p2_chat_id"] == -200 and game["p2_message_id"] == 900
        assert context.bot.send_message.calls == []  # 不需要私聊
        markup = joiner.message.reply_text.calls[0][1]["reply_markup"]
        assert markup.inline_keyboard[0][0].callback_data == f"rps_choice_{game['id']}_rock_2"
        assert total_coins(players, 1) == 4 and total_coins(players, 2) == 4

    def test_joining_through_the_button_works_and_a_stale_invitation_does_not(self, players):
        self.open_room()
        stale_update, stale_answer, _ = join_click(2, message_id=WAITING_MESSAGE + 9)
        context = make_game_context()

        run(rps.rps_callback_handler(stale_update, context))
        assert "已开始或已被取消" in stale_answer.texts[-1]
        assert fetch_scalar(players, "SELECT COUNT(*) FROM rps_games") == 0

        update, answer, _ = join_click(2)
        run(rps.rps_callback_handler(update, context))

        assert fetch_scalar(players, "SELECT COUNT(*) FROM rps_games") == 1
        assert total_coins(players, 1) == 4 and total_coins(players, 2) == 4

    def test_a_joiner_without_coins_cannot_start_a_game(self, app_database):
        seed_user(app_database, 1, free=5)
        seed_user(app_database, 2, free=0)
        self.open_room()
        joiner = command(2)

        run(rps.rps_game_command.__wrapped__(joiner, make_game_context()))

        assert "金币不足" in joiner.message.reply_text.texts[-1]
        assert fetch_scalar(app_database, "SELECT COUNT(*) FROM rps_games") == 0
        assert total_coins(app_database, 1) == 5
        assert rps.waiting_room is not None

    def test_a_waiting_player_who_spent_their_coins_loses_the_room_and_the_joiner_keeps_theirs(
        self, players
    ):
        self.open_room()
        execute(players, "UPDATE `user` SET coins = 0 WHERE id = 1")
        joiner = command(2)
        context = make_game_context()

        run(rps.rps_game_command.__wrapped__(joiner, context))

        assert "邀请已取消" in joiner.message.reply_text.texts[-1]
        assert rps.waiting_room is None
        assert fetch_scalar(players, "SELECT COUNT(*) FROM rps_games") == 0
        assert total_coins(players, 2) == 5
        assert "邀请已取消" in context.bot.edit_message_text.texts[-1]

    def test_a_panel_that_cannot_be_delivered_cancels_the_game_and_refunds_both(self, players):
        self.open_room()
        context = make_game_context(send=MessageSender(fail_times=1))
        joiner = command(2)

        run(rps.rps_game_command.__wrapped__(joiner, context))

        game = fetch(players, "SELECT * FROM rps_games")[0]
        assert game["status"] == "refunded" and game["outcome"] == "failed"
        assert total_coins(players, 1) == 5 and total_coins(players, 2) == 5
        assert "创建游戏失败" in joiner.message.reply_text.texts[-1]
        assert rps.waiting_room is None
        assert context.job_queue.jobs == []  # 没有为失败的对局安排超时
        assert "创建失败" in context.bot.edit_message_text.texts[-1]

    def test_a_player_in_a_game_cannot_open_another_room(self, players):
        self.open_room()
        run(rps.rps_game_command.__wrapped__(command(2), make_game_context()))
        again = command(1)

        run(rps.rps_game_command.__wrapped__(again, make_game_context()))

        assert "已经在一个游戏中" in again.message.reply_text.texts[-1]
        assert rps.waiting_room is None

    def play_to_choices(self, players):
        self.open_room()
        run(rps.rps_game_command.__wrapped__(command(2), make_game_context(send=MessageSender(first_id=700))))
        return fetch(players, "SELECT * FROM rps_games")[0]

    def choice_click(self, game, user_id, choice, *, button_user=None):
        private = game["p1_private_msg_id"] if user_id == 1 else game["p2_private_msg_id"]
        return make_callback_update(
            from_user_id=user_id,
            data=f"rps_choice_{game['id']}_{choice}_{button_user or user_id}",
            chat_id=user_id,
            message_id=private,
        )

    def test_choosing_updates_the_panels_and_the_second_choice_announces_the_result(self, players):
        game = self.play_to_choices(players)
        context = make_game_context()

        first_update, first_answer, _ = self.choice_click(game, 1, "rock")
        run(rps.rps_callback_handler(first_update, context))
        assert "您选择了" in first_answer.texts[-1]
        progress = context.bot.edit_message_text.texts
        assert any("玩家1: @user1 ✓ 已选择" in text for text in progress)
        assert any("等待对方做出选择" in text for text in progress)

        second_update, _, _ = self.choice_click(game, 2, "scissors")
        run(rps.rps_callback_handler(second_update, context))

        assert "@user1 获胜" in context.bot.edit_message_text.texts[-1]
        assert game_row(players, game["id"])["announced_at"] is not None
        assert total_coins(players, 1) == 6 and total_coins(players, 2) == 4

    def test_buttons_of_an_older_format_or_another_player_do_nothing(self, players):
        game = self.play_to_choices(players)
        context = make_game_context()

        legacy = make_callback_update(from_user_id=1, data="rps_choice_rock_1", chat_id=1, message_id=700)
        run(rps.rps_callback_handler(legacy[0], context))
        foreign_update, foreign_answer, _ = self.choice_click(game, 2, "rock", button_user=1)
        run(rps.rps_callback_handler(foreign_update, context))

        assert "失效" in legacy[1].texts[-1]
        assert "这不是您的按钮" in foreign_answer.texts[-1]
        assert game_row(players, game["id"])["p1_choice"] is None
        assert game_row(players, game["id"])["p2_choice"] is None

    def test_a_button_from_a_finished_game_does_not_touch_the_next_one(self, players):
        game = self.play_to_choices(players)
        context = make_game_context()
        for user, choice in ((1, "rock"), (2, "rock")):  # 平局收场
            update, _, _ = self.choice_click(game, user, choice)
            run(rps.rps_callback_handler(update, context))
        assert total_coins(players, 1) == 5

        self.open_room()
        run(rps.rps_game_command.__wrapped__(command(2), make_game_context(send=MessageSender(first_id=800))))
        stale_update, stale_answer, _ = self.choice_click(game, 1, "paper")
        run(rps.rps_callback_handler(stale_update, context))

        assert "游戏已经结束" in stale_answer.texts[-1]
        newest = fetch(players, "SELECT * FROM rps_games ORDER BY id DESC LIMIT 1")[0]
        assert newest["id"] != game["id"] and newest["p1_choice"] is None

    def test_cancelling_the_waiting_room_costs_nothing(self, players):
        self.open_room()
        update, answer, edit = make_callback_update(
            from_user_id=1, data="rps_cancel", chat_id=CHAT, message_id=WAITING_MESSAGE
        )

        run(rps.rps_callback_handler(update, make_game_context()))

        assert rps.waiting_room is None
        assert "已取消" in edit.texts[-1]
        assert total_coins(players, 1) == 5

    def test_the_waiting_room_expires_by_timer(self, players):
        _, context = self.open_room()
        job_context = make_job_context(make_game_bot(edit=Recorder()), (1, WAITING_MESSAGE))

        run(rps.cancel_waiting_job(job_context))

        assert rps.waiting_room is None
        assert "超时取消" in job_context.bot.edit_message_text.texts[-1]
