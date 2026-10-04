"""游戏的纯逻辑：回调数据的解析、胜负判定、op_key 派生、面板文案（不访问数据库）。"""

import pytest

from core import balance
from features.games import gamble, gamble_rounds, rockpaperscissors_game as rps, rps_games
from features.games.rpg import settlement


class TestGambleCallbackData:
    def test_round_trips_the_round_id_and_the_amount(self):
        assert gamble.parse_callback_data(gamble.callback_data(42, 10)) == (42, 10)

    @pytest.mark.parametrize(
        "data", [None, "", "gamble", "gamble_5", "gamble_x_5", "gamble_1_2_3", "other_1_5"]
    )
    def test_old_or_malformed_data_is_rejected(self, data):
        assert gamble.parse_callback_data(data) is None

    def test_every_button_carries_the_round_id(self):
        buttons = [b.callback_data for b in gamble.build_keyboard(7).inline_keyboard[0]]

        assert buttons == [f"gamble_7_{amount}" for amount in gamble_rounds.BET_AMOUNTS]


class TestGambleTexts:
    def make_settlement(self, *, status, winner_id=None, prize=0, bets=()):
        current = gamble_rounds.Round(
            id=1,
            chat_id=-1,
            message_id=2,
            status=status,
            seconds_left=0.0,
            winner_id=winner_id,
            prize=prize,
            announced=False,
        )
        return gamble_rounds.Settlement(current, tuple(bets), transitioned=True)

    def test_the_result_names_the_winner_and_lists_the_bets(self):
        bets = (
            gamble_rounds.Bet(1, "alice", 5, "k1"),
            gamble_rounds.Bet(2, "bob", 20, "k2"),
        )

        text = gamble.result_text(
            self.make_settlement(status="settled", winner_id=2, prize=25, bets=bets)
        )

        assert "中奖者：@bob" in text and "25 金币" in text
        assert "@alice 押注 5 金币" in text and "@bob 押注 20 金币" in text

    def test_an_empty_round_says_nobody_took_part(self):
        assert "无人参与" in gamble.result_text(self.make_settlement(status="settled"))

    def test_a_refunded_round_says_the_bets_were_returned(self):
        bets = (gamble_rounds.Bet(1, "alice", 5, "k1"),)

        text = gamble.result_text(self.make_settlement(status="refunded", bets=bets))

        assert "退还" in text and "@alice" in text


class TestGambleDraw:
    def test_a_single_bettor_always_wins(self):
        only = gamble_rounds.Bet(1, "alice", 5, "k1")

        assert gamble_rounds.draw_winner((only,)) is only

    def test_op_keys_are_derived_from_the_round(self):
        assert gamble_rounds.bet_op_key(12, 34) == "gamble:12:bet:34"
        assert gamble_rounds.payout_op_key(12) == "gamble:12:payout"


class TestRockPaperScissors:
    def test_choice_buttons_are_bound_to_the_game_and_the_player(self):
        buttons = [b.callback_data for b in rps.get_choice_keyboard(9, 5).inline_keyboard[0]]

        assert buttons == ["rps_choice_9_rock_5", "rps_choice_9_scissors_5", "rps_choice_9_paper_5"]
        assert [rps.parse_choice_callback(data) for data in buttons] == [
            (9, "rock", 5),
            (9, "scissors", 5),
            (9, "paper", 5),
        ]

    @pytest.mark.parametrize(
        "data",
        [None, "", "rps_choice_rock_5", "rps_choice_9_lizard_5", "rps_choice_x_rock_5", "rps_join"],
    )
    def test_old_or_malformed_choice_data_is_rejected(self, data):
        assert rps.parse_choice_callback(data) is None

    def test_op_keys_are_derived_from_the_game(self):
        assert rps_games.entry_op_key(7, 1) == "rps:7:entry:1"
        assert rps_games.payout_op_key(7) == "rps:7:win"
        assert balance.refund_op_key(rps_games.entry_op_key(7, 1)) == "refund:rps:7:entry:1"

    def test_the_final_text_depends_on_the_outcome(self):
        def game(outcome, c1=None, c2=None):
            return rps_games.Game(
                id=1,
                status="refunded" if outcome in ("timeout", "failed") else "settled",
                outcome=outcome,
                same_chat=True,
                p1=rps_games.Seat(1, "alice", 1, choice=c1),
                p2=rps_games.Seat(2, "bob", 1, choice=c2),
                seconds_left=0.0,
                announced=False,
            )

        assert "@alice 获胜" in rps.final_text(game("p1", "rock", "scissors"))
        assert "@bob 获胜" in rps.final_text(game("p2", "rock", "paper"))
        assert "平局" in rps.final_text(game("draw", "rock", "rock"))
        timeout = rps.final_text(game("timeout", "rock", None))
        assert "已超时" in timeout and "玩家1: @alice 已选择" in timeout and "玩家2: @bob 未选择" in timeout
        assert "失败" in rps.final_text(game("failed"))


class TestRpgOpKeys:
    def test_keys_follow_the_command_message(self):
        assert settlement.heal_op_key(7, 21) == "rpg:heal:7:21"
        assert settlement.monster_reward_op_key(7, 21) == "rpg:monster:7:21:reward"
        assert settlement.pvp_loss_op_key(-100, 21) == "rpg:pvp:-100:21:loss"
        assert settlement.pvp_win_op_key(-100, 21) == "rpg:pvp:-100:21:win"
