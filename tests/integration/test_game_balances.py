"""骰宝、御神签、RPG 的金币路径：扣款失败、重放、事务回滚（真实 MySQL）。"""

from datetime import date, datetime
from types import SimpleNamespace
from unittest import mock

import pytest
from economy_support import (
    Recorder,
    gather_all,
    ledger_rows,
    make_callback_update,
    make_command_update,
    make_context,
    seed_user,
    total_coins,
    user_state,
)
from game_support import seed_character
from mysql_support import execute, fetch, fetch_scalar, run

from fogmoe_telegram_bot.core import balance
from fogmoe_telegram_bot.features.games import omikuji, sicbo
from fogmoe_telegram_bot.features.games.rpg import battles, monsters, settlement
from fogmoe_telegram_bot.features.games.rpg.characters import heal_character

TODAY = date(2026, 10, 5)


def keys(url, user_id=None):
    return [(r["op_key"], r["kind"]) for r in ledger_rows(url, user_id)]


def character(url, user_id):
    return fetch(url, "SELECT hp, max_hp, experience FROM rpg_characters WHERE user_id = %s", (user_id,))[0]


# ---------------------------------------------------------------------------
# 骰宝
# ---------------------------------------------------------------------------

PANEL = 40


@pytest.fixture
def sicbo_game(monkeypatch):
    monkeypatch.setattr(sicbo, "active_games", {})
    monkeypatch.setattr(sicbo, "game_locks", {})

    def open_game(user_id=1, *, bet_type="big", message_id=PANEL):
        sicbo.active_games[user_id] = {
            "bet_type": bet_type,
            "bet_amount": 0,
            "message_id": message_id,
            "start_time": datetime.now(),
        }

    return open_game


def force_dice(monkeypatch, *dice):
    real_roll = sicbo.roll_dice

    def fake_roll():
        with mock.patch.object(sicbo.random, "randint", side_effect=list(dice)):
            return real_roll()

    monkeypatch.setattr(sicbo, "roll_dice", fake_roll)


def sicbo_click(user_id, action, *, message_id=PANEL):
    update, answer, edit = make_callback_update(
        from_user_id=user_id,
        data=f"sicbo_{user_id}_{action}",
        chat_id=user_id,
        message_id=message_id,
    )
    run(sicbo.handle_callback(update, make_context()))
    return answer, edit


class TestSicbo:
    def test_a_win_debits_the_bet_and_credits_the_winnings_in_one_transaction(
        self, app_database, sicbo_game, monkeypatch
    ):
        seed_user(app_database, 1, free=50)
        sicbo_game()
        force_dice(monkeypatch, 3, 4, 5)  # 总和 12：大

        _, edit = sicbo_click(1, "amount_10")

        assert total_coins(app_database, 1) == 50 - 10 + 20
        assert keys(app_database) == [
            (f"sicbo:1:{PANEL}:bet", "debit"),
            (f"sicbo:1:{PANEL}:win", "credit"),
        ]
        assert "恭喜您赢了" in edit.texts[-1] and "当前余额: 60 金币" in edit.texts[-1]
        assert 1 not in sicbo.active_games

    def test_a_loss_only_debits_the_bet(self, app_database, sicbo_game, monkeypatch):
        seed_user(app_database, 1, free=50)
        sicbo_game()
        force_dice(monkeypatch, 1, 1, 2)  # 总和 4：小

        _, edit = sicbo_click(1, "amount_10")

        assert total_coins(app_database, 1) == 40
        assert keys(app_database) == [(f"sicbo:1:{PANEL}:bet", "debit")]
        assert "您损失了 10 金币" in edit.texts[-1]

    def test_an_insufficient_balance_leaves_everything_untouched(self, app_database, sicbo_game):
        seed_user(app_database, 1, free=5)
        sicbo_game()

        _, edit = sicbo_click(1, "amount_10")

        assert "您的金币不足！您只有 5 金币" in edit.texts[-1]
        assert total_coins(app_database, 1) == 5
        assert ledger_rows(app_database) == []
        assert 1 not in sicbo.active_games

    def test_a_failed_payout_rolls_the_bet_back_too(self, app_database, sicbo_game, monkeypatch):
        seed_user(app_database, 1, free=50)
        sicbo_game()
        force_dice(monkeypatch, 3, 4, 5)

        async def broken_credit(*args, **kwargs):
            raise RuntimeError("injected payout failure")

        monkeypatch.setattr(balance, "credit", broken_credit)
        _, edit = sicbo_click(1, "amount_10")

        assert "出现错误" in edit.texts[-1]
        assert total_coins(app_database, 1) == 50
        assert ledger_rows(app_database) == []

    def test_settling_the_same_panel_twice_charges_once(self, app_database):
        seed_user(app_database, 1, free=50)

        first = run(sicbo.settle_bet(1, 1, PANEL, 10, 0))
        with pytest.raises(sicbo.AlreadySettled):
            run(sicbo.settle_bet(1, 1, PANEL, 10, 20))

        assert first == 40
        assert total_coins(app_database, 1) == 40
        assert keys(app_database) == [(f"sicbo:1:{PANEL}:bet", "debit")]

    def test_a_replayed_click_reports_the_game_as_settled(self, app_database, sicbo_game):
        seed_user(app_database, 1, free=50)
        run(sicbo.settle_bet(1, 1, PANEL, 10, 0))
        sicbo_game()

        _, edit = sicbo_click(1, "amount_10")

        assert "已经结算过" in edit.texts[-1]
        assert total_coins(app_database, 1) == 40

    def test_a_panel_that_is_not_the_games_current_one_is_rejected(self, app_database, sicbo_game):
        seed_user(app_database, 1, free=50)
        sicbo_game()

        answer, _ = sicbo_click(1, "amount_10", message_id=PANEL + 1)

        assert "已失效" in answer.texts[-1]
        assert total_coins(app_database, 1) == 50
        assert ledger_rows(app_database) == []

    @pytest.mark.parametrize("action", ["amount_7", "amount_-5", "amount_0"])
    def test_amounts_that_are_not_on_the_keyboard_are_rejected(self, app_database, sicbo_game, action):
        seed_user(app_database, 1, free=50)
        sicbo_game()

        answer, _ = sicbo_click(1, action)

        assert answer.texts
        assert total_coins(app_database, 1) == 50
        assert ledger_rows(app_database) == []

    def test_an_amount_before_choosing_a_bet_type_is_rejected(self, app_database, sicbo_game):
        seed_user(app_database, 1, free=50)
        sicbo_game(bet_type=None)

        sicbo_click(1, "amount_10")

        assert total_coins(app_database, 1) == 50
        assert ledger_rows(app_database) == []


# ---------------------------------------------------------------------------
# 御神签
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_omikuji_throttle(monkeypatch):
    monkeypatch.setattr(omikuji, "omikuji_locks", {})


def omikuji_rows(url):
    return fetch(url, "SELECT user_id, fortune_date, fortune FROM user_omikuji")


class TestOmikuji:
    def test_a_draw_costs_one_coin_and_registers_the_fortune_with_it(self, app_database):
        seed_user(app_database, 1, free=3)

        status, fortune = run(omikuji.draw_daily_fortune(1, today=TODAY))

        assert status == omikuji.DRAW_NEW and fortune == omikuji.get_daily_fortune(1, TODAY)
        assert total_coins(app_database, 1) == 2
        assert keys(app_database) == [("omikuji:1:2026-10-05", "debit")]
        assert [(r["user_id"], r["fortune_date"], r["fortune"]) for r in omikuji_rows(app_database)] == [
            (1, TODAY, fortune)
        ]

    def test_drawing_again_the_same_day_returns_the_registered_fortune_for_free(self, app_database):
        seed_user(app_database, 1, free=3)
        _, fortune = run(omikuji.draw_daily_fortune(1, today=TODAY))

        again = run(omikuji.draw_daily_fortune(1, today=TODAY))

        assert again == (omikuji.DRAW_ALREADY, fortune)
        assert total_coins(app_database, 1) == 2
        assert len(ledger_rows(app_database)) == 1

    def test_a_new_day_is_a_new_draw(self, app_database):
        seed_user(app_database, 1, free=3)
        run(omikuji.draw_daily_fortune(1, today=TODAY))

        status, _ = run(omikuji.draw_daily_fortune(1, today=date(2026, 10, 6)))

        assert status == omikuji.DRAW_NEW
        assert total_coins(app_database, 1) == 1

    def test_concurrent_draws_charge_once(self, app_database):
        seed_user(app_database, 1, free=3)

        async def scenario():
            return await gather_all(
                *(omikuji.draw_daily_fortune(1, today=TODAY) for _ in range(3))
            )

        results = run(scenario())

        assert all(isinstance(r, tuple) for r in results), results
        assert sorted(r[0] for r in results) == [
            omikuji.DRAW_ALREADY,
            omikuji.DRAW_ALREADY,
            omikuji.DRAW_NEW,
        ]
        assert len({r[1] for r in results}) == 1
        assert total_coins(app_database, 1) == 2
        assert len(omikuji_rows(app_database)) == 1

    def test_an_empty_wallet_draws_nothing(self, app_database):
        seed_user(app_database, 1, free=0)

        result = run(omikuji.draw_daily_fortune(1, today=TODAY))

        assert result == (omikuji.DRAW_INSUFFICIENT, None)
        assert ledger_rows(app_database) == []
        assert omikuji_rows(app_database) == []

    def test_an_unregistered_user_draws_nothing(self, app_database):
        assert run(omikuji.draw_daily_fortune(99, today=TODAY)) == (omikuji.DRAW_INSUFFICIENT, None)

    def test_a_failure_while_registering_the_fortune_rolls_the_debit_back(
        self, app_database, monkeypatch
    ):
        seed_user(app_database, 1, free=3)
        # 超出 VARCHAR(10)：strict 模式下 INSERT 失败，已经执行的扣款必须一起回滚
        monkeypatch.setattr(omikuji, "get_daily_fortune", lambda user_id, today=None: "x" * 40)

        with pytest.raises(Exception):
            run(omikuji.draw_daily_fortune(1, today=TODAY))

        assert total_coins(app_database, 1) == 3
        assert ledger_rows(app_database) == []
        assert omikuji_rows(app_database) == []

    def test_the_command_charges_once_and_replies_with_the_fortune(self, app_database):
        seed_user(app_database, 1, free=3)
        update = make_command_update(user_id=1, chat_id=1, message_id=9)

        run(omikuji.omikuji_command.__wrapped__(update, make_context()))

        assert "的今日运势" in update.message.reply_text.texts[-1]
        assert total_coins(app_database, 1) == 2
        assert len(omikuji_rows(app_database)) == 1

    def test_a_reply_failure_after_the_draw_costs_nothing_extra_on_the_next_try(self, app_database):
        seed_user(app_database, 1, free=3)
        broken = make_command_update(
            user_id=1, chat_id=1, message_id=9, reply_text=Recorder(fail_times=1)
        )
        run(omikuji.omikuji_command.__wrapped__(broken, make_context()))
        omikuji.omikuji_locks.clear()
        retry = make_command_update(user_id=1, chat_id=1, message_id=10)

        run(omikuji.omikuji_command.__wrapped__(retry, make_context()))

        assert "已经抽过御神签" in retry.message.reply_text.texts[-1]
        assert total_coins(app_database, 1) == 2
        assert len(ledger_rows(app_database)) == 1

    def test_the_command_refuses_politely_when_the_user_cannot_pay(self, app_database):
        seed_user(app_database, 1, free=0)
        update = make_command_update(user_id=1, chat_id=1, message_id=9)

        run(omikuji.omikuji_command.__wrapped__(update, make_context()))

        assert "没有足够的金币" in update.message.reply_text.texts[-1]
        assert omikuji_rows(app_database) == []
        assert ledger_rows(app_database) == []


# ---------------------------------------------------------------------------
# RPG：回血
# ---------------------------------------------------------------------------

CHAT = 7
MESSAGE = 21


class TestHeal:
    def heal(self, url, user_id=1, *, message_id=MESSAGE):
        return run(
            settlement.heal_for_coins(user_id, settlement.heal_op_key(CHAT, message_id))
        )

    def test_healing_charges_ten_coins_and_restores_hp_together(self, app_database):
        seed_user(app_database, 1, free=15)
        seed_character(app_database, 1, hp=3, max_hp=10)

        result = self.heal(app_database)

        assert result.status == settlement.HEAL_DONE and result.max_hp == 10
        assert total_coins(app_database, 1) == 5
        assert character(app_database, 1)["hp"] == 10
        assert keys(app_database) == [(f"rpg:heal:{CHAT}:{MESSAGE}", "debit")]

    def test_an_insufficient_balance_changes_nothing(self, app_database):
        seed_user(app_database, 1, free=5)
        seed_character(app_database, 1, hp=3, max_hp=10)

        result = self.heal(app_database)

        assert result.status == settlement.HEAL_INSUFFICIENT and result.balance_total == 5
        assert character(app_database, 1)["hp"] == 3
        assert ledger_rows(app_database) == []

    def test_a_full_health_bar_is_not_charged(self, app_database):
        seed_user(app_database, 1, free=15)
        seed_character(app_database, 1, hp=10, max_hp=10)

        assert self.heal(app_database).status == settlement.HEAL_FULL
        assert total_coins(app_database, 1) == 15

    def test_a_user_without_a_character_is_not_charged(self, app_database):
        seed_user(app_database, 1, free=15)

        assert self.heal(app_database).status == settlement.HEAL_NO_CHARACTER
        assert total_coins(app_database, 1) == 15

    def test_the_same_command_delivered_twice_is_charged_once(self, app_database):
        seed_user(app_database, 1, free=30)
        seed_character(app_database, 1, hp=3, max_hp=10)
        self.heal(app_database)
        execute(app_database, ("UPDATE rpg_characters SET hp = 1 WHERE user_id = %s", (1,)))

        replay = self.heal(app_database)

        assert replay.status == settlement.HEAL_REPLAY
        assert total_coins(app_database, 1) == 20
        assert character(app_database, 1)["hp"] == 1

    def test_a_failure_while_restoring_hp_rolls_the_charge_back(self, app_database, monkeypatch):
        seed_user(app_database, 1, free=15)
        seed_character(app_database, 1, hp=3, max_hp=10)

        async def broken(*args, **kwargs):
            raise RuntimeError("injected failure")

        monkeypatch.setattr(settlement, "set_character_fields", broken)
        with pytest.raises(RuntimeError):
            self.heal(app_database)

        assert total_coins(app_database, 1) == 15
        assert character(app_database, 1)["hp"] == 3
        assert ledger_rows(app_database) == []

    def test_concurrent_heals_charge_once(self, app_database):
        seed_user(app_database, 1, free=30)
        seed_character(app_database, 1, hp=3, max_hp=10)

        async def scenario():
            return await gather_all(
                *(
                    settlement.heal_for_coins(1, settlement.heal_op_key(CHAT, message_id))
                    for message_id in (MESSAGE, MESSAGE + 1, MESSAGE + 2)
                )
            )

        results = run(scenario())

        assert sorted(r.status for r in results) == [
            settlement.HEAL_DONE,
            settlement.HEAL_FULL,
            settlement.HEAL_FULL,
        ]
        assert total_coins(app_database, 1) == 20

    def test_the_command_reports_the_outcome(self, app_database):
        seed_user(app_database, 1, free=15)
        seed_character(app_database, 1, hp=3, max_hp=10)
        update = make_command_update(user_id=1, chat_id=CHAT, message_id=MESSAGE)

        run(heal_character(update, make_context()))

        assert "花费 10 金币恢复了生命值" in update.message.reply_text.texts[-1]
        assert "当前HP: 10/10" in update.message.reply_text.texts[-1]
        assert total_coins(app_database, 1) == 5


# ---------------------------------------------------------------------------
# RPG：怪物
# ---------------------------------------------------------------------------


class TestMonsterBattle:
    def settle(self, *, won=True, hp=7, message_id=MESSAGE):
        return run(
            settlement.settle_monster_battle(
                1,
                chat_id=CHAT,
                message_id=message_id,
                player_hp=hp,
                won=won,
                exp_reward=15,
                coin_reward=2,
            )
        )

    def test_a_victory_grants_coins_experience_and_hp_together(self, app_database):
        seed_user(app_database, 1, free=0)
        seed_character(app_database, 1, hp=10, experience=5)

        assert self.settle() is True

        assert total_coins(app_database, 1) == 2
        assert character(app_database, 1) == {"hp": 7, "max_hp": 10, "experience": 20}
        assert keys(app_database) == [(f"rpg:monster:{CHAT}:{MESSAGE}:reward", "credit")]

    def test_a_defeat_only_updates_hp(self, app_database):
        seed_user(app_database, 1, free=0)
        seed_character(app_database, 1, hp=10, experience=5)

        assert self.settle(won=False, hp=0) is True

        assert total_coins(app_database, 1) == 0
        assert character(app_database, 1) == {"hp": 0, "max_hp": 10, "experience": 5}
        assert ledger_rows(app_database) == []

    def test_the_same_command_delivered_twice_is_rewarded_once(self, app_database):
        seed_user(app_database, 1, free=0)
        seed_character(app_database, 1, hp=10, experience=5)
        self.settle()

        assert self.settle() is False

        assert total_coins(app_database, 1) == 2
        assert character(app_database, 1)["experience"] == 20

    def test_a_failure_after_the_credit_rolls_the_reward_back(self, app_database, monkeypatch):
        seed_user(app_database, 1, free=0)
        seed_character(app_database, 1, hp=10, experience=5)

        async def broken(*args, **kwargs):
            raise RuntimeError("injected failure")

        monkeypatch.setattr(settlement, "set_character_fields", broken)
        with pytest.raises(RuntimeError):
            self.settle()

        assert total_coins(app_database, 1) == 0
        assert character(app_database, 1) == {"hp": 10, "max_hp": 10, "experience": 5}
        assert ledger_rows(app_database) == []

    def test_the_battle_command_settles_once_even_when_redelivered(self, app_database, monkeypatch):
        monkeypatch.setattr(monsters, "monster_battle_cooldowns", {})
        seed_user(app_database, 1, free=0)
        seed_character(app_database, 1, hp=10, atk=12, defense=5)

        def fight():
            update = make_command_update(user_id=1, chat_id=CHAT, message_id=MESSAGE)
            run(monsters.initiate_monster_battle(update, make_context(), "goblin"))
            monsters.monster_battle_cooldowns.clear()
            return update

        first = fight()
        second = fight()

        assert "获得 2 枚金币" in first.message.reply_text.texts[-1]
        assert "已经结算过" in second.message.reply_text.texts[-1]
        assert total_coins(app_database, 1) == 2
        assert character(app_database, 1)["experience"] == 15


# ---------------------------------------------------------------------------
# RPG：玩家对战
# ---------------------------------------------------------------------------


class TestPlayerBattle:
    def settle(self, *, message_id=MESSAGE, exp_gain=50, hp_after=None):
        return run(
            settlement.settle_player_battle(
                chat_id=CHAT,
                message_id=message_id,
                winner_id=1,
                loser_id=2,
                exp_gain=exp_gain,
                hp_after=hp_after or {1: 8, 2: 0},
            )
        )

    @pytest.fixture
    def fighters(self, app_database):
        seed_user(app_database, 1, free=10)
        seed_user(app_database, 2, free=70, paid=30)
        seed_character(app_database, 1, hp=10)
        seed_character(app_database, 2, hp=10)
        return app_database

    def test_the_loser_pays_ten_percent_and_the_winner_gets_eighty_percent_of_it(self, fighters):
        result = self.settle()

        assert result == settlement.PvpSettlement(True, 10, 8)
        assert total_coins(fighters, 2) == 90
        assert total_coins(fighters, 1) == 18
        assert keys(fighters, 2) == [(f"rpg:pvp:{CHAT}:{MESSAGE}:loss", "debit")]
        assert keys(fighters, 1) == [(f"rpg:pvp:{CHAT}:{MESSAGE}:win", "credit")]
        assert character(fighters, 1) == {"hp": 8, "max_hp": 10, "experience": 50}
        assert character(fighters, 2)["hp"] == 0

    def test_the_amount_follows_the_balance_at_settlement_time(self, fighters):
        execute(fighters, "UPDATE `user` SET coins = 20, coins_paid = 0 WHERE id = 2")

        result = self.settle()

        assert (result.coins_lost, result.coins_to_winner) == (2, 1)
        assert total_coins(fighters, 2) == 18

    def test_a_poor_loser_loses_nothing_but_the_winner_still_gains_experience(self, fighters):
        execute(fighters, "UPDATE `user` SET coins = 5, coins_paid = 0 WHERE id = 2")

        result = self.settle()

        assert (result.applied, result.coins_lost, result.coins_to_winner) == (True, 0, 0)
        assert ledger_rows(fighters) == []
        assert character(fighters, 1)["experience"] == 50

    def test_the_same_battle_delivered_twice_settles_once(self, fighters):
        self.settle()

        replay = self.settle()

        assert replay == settlement.PvpSettlement(False, 10, 8)
        assert total_coins(fighters, 2) == 90 and total_coins(fighters, 1) == 18
        assert character(fighters, 1)["experience"] == 50
        assert len(ledger_rows(fighters)) == 2

    def test_a_failure_while_crediting_the_winner_rolls_the_loser_back(self, fighters, monkeypatch):
        async def broken_credit(*args, **kwargs):
            raise RuntimeError("injected failure")

        monkeypatch.setattr(balance, "credit", broken_credit)
        with pytest.raises(RuntimeError):
            self.settle()

        assert total_coins(fighters, 2) == 100
        assert total_coins(fighters, 1) == 10
        assert ledger_rows(fighters) == []
        assert character(fighters, 1) == {"hp": 10, "max_hp": 10, "experience": 0}

    def test_concurrent_settlements_of_one_battle_apply_once(self, fighters):
        async def scenario():
            return await gather_all(
                *(
                    settlement.settle_player_battle(
                        chat_id=CHAT,
                        message_id=MESSAGE,
                        winner_id=1,
                        loser_id=2,
                        exp_gain=50,
                        hp_after={1: 8, 2: 0},
                    )
                    for _ in range(3)
                )
            )

        results = run(scenario())

        assert all(isinstance(r, settlement.PvpSettlement) for r in results), results
        assert sorted(r.applied for r in results) == [False, False, True]
        assert total_coins(fighters, 2) == 90 and total_coins(fighters, 1) == 18

    def test_the_battle_flow_settles_through_the_ledger(self, fighters, monkeypatch):
        execute(fighters, "UPDATE rpg_characters SET atk = 0 WHERE user_id = 2")
        execute(fighters, "UPDATE rpg_characters SET atk = 20 WHERE user_id = 1")
        chat = SimpleNamespace(username="someone", first_name="Someone")
        context = make_context(get_chat=Recorder(result=chat))

        def battle():
            update = make_command_update(user_id=1, chat_id=CHAT, message_id=MESSAGE)
            run(battles.run_battle(update, context, 1, 2))
            return update

        first = battle()
        second = battle()

        assert "损失了 10" in first.message.reply_text.texts[-1]
        assert "已经结算过" in second.message.reply_text.texts[-1]
        assert total_coins(fighters, 2) == 90 and total_coins(fighters, 1) == 18
        assert len(ledger_rows(fighters)) == 2
        assert fetch_scalar(
            fighters, "SELECT experience FROM rpg_characters WHERE user_id = 1"
        ) > 0
        assert user_state(fighters, 2)["paid"] + user_state(fighters, 2)["free"] == 90
