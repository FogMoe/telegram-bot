"""经济入口的纯逻辑：手续费、保底、权限升级规则、赎回文案与各路径的 op_key 派生。"""

from datetime import date, datetime

import pytest

from core import balance
from features.ai.tools import user_tools
from features.crypto import crypto_predict
from features.crypto import swap_fogmoe_solana_token as swap
from features.economy import bribe, coins, ref, shop, stake_coin, task


class FakeRandom:
    def __init__(self, value):
        self.value = value

    def random(self):
        return self.value

    def randint(self, low, high):
        return self.value


class TestGiveFee:
    @pytest.mark.parametrize(
        ("amount", "fee"),
        [(1, 0), (2, 1), (4, 1), (5, 1), (9, 1), (10, 2), (99, 19), (100, 20), (1000, 200)],
    )
    def test_fee_is_a_fifth_with_a_minimum_of_one_coin(self, amount, fee):
        assert coins._calculate_give_fee(amount) == fee


class TestShopRules:
    @pytest.mark.parametrize(
        ("current", "target", "refusal"),
        [
            (0, 1, None),
            (1, 1, "您已经拥有权限或已升级。"),
            (3, 1, "您已经拥有权限或已升级。"),
            (0, 2, "您需要先升级到1级权限。"),
            (1, 2, None),
            (2, 2, "您已经拥有2级或更高权限。"),
            (1, 3, "您需要先升级到2级权限。"),
            (2, 3, None),
            (3, 3, "您已经拥有3级或更高权限。"),
        ],
    )
    def test_permission_upgrades_must_follow_the_levels(self, current, target, refusal):
        assert shop.permission_upgrade_refusal(current, target) == refusal

    def test_upgrade_prices_and_buttons_cover_the_same_levels(self):
        assert shop.PERMISSION_UPGRADE_PRICES == {1: 50, 2: 100, 3: 10000}
        assert set(shop.UPGRADE_CALLBACKS.values()) == set(shop.PERMISSION_UPGRADE_PRICES)

    @pytest.mark.parametrize(
        ("roll", "reward"),
        [(0.0, 0), (0.7999, 0), (0.8, 1), (0.9899, 1), (0.99, 5), (0.9994, 5), (0.9995, 100), (0.99999, 100)],
    )
    def test_huanle_reward_follows_the_published_odds(self, roll, reward):
        assert shop.draw_huanle_reward(FakeRandom(roll)) == reward

    def test_scratch_reward_is_drawn_from_zero_to_twenty(self):
        rewards = {shop.draw_scratch_reward() for _ in range(500)}

        assert min(rewards) >= 0 and max(rewards) <= 20

    def test_pity_counts_consecutive_misses_and_pays_the_bonus_at_the_threshold(self):
        today = date(2026, 10, 5)
        record = None
        triggered = []
        for _ in range(5):
            record, bonus = shop.advance_pity(record, today=today, miss=True, threshold=5)
            triggered.append(bonus)

        assert triggered == [False, False, False, False, True]
        assert record == {"count": 0, "date": today}

    def test_a_hit_resets_the_streak(self):
        today = date(2026, 10, 5)
        record, _ = shop.advance_pity(None, today=today, miss=True, threshold=5)
        record, _ = shop.advance_pity(record, today=today, miss=True, threshold=5)

        record, bonus = shop.advance_pity(record, today=today, miss=False, threshold=5)

        assert (record["count"], bonus) == (0, False)

    def test_the_streak_starts_over_on_a_new_day(self):
        yesterday = {"count": 4, "date": date(2026, 10, 4)}

        record, bonus = shop.advance_pity(
            yesterday, today=date(2026, 10, 5), miss=True, threshold=5
        )

        assert (record["count"], bonus) == (1, False)

    def test_advance_pity_does_not_modify_the_stored_record(self):
        stored = {"count": 2, "date": date(2026, 10, 5)}

        shop.advance_pity(stored, today=date(2026, 10, 5), miss=True, threshold=5)

        assert stored == {"count": 2, "date": date(2026, 10, 5)}


class TestWithdrawMessage:
    def outcome(self, **kwargs):
        defaults = dict(
            status=stake_coin.WithdrawStatus.WITHDRAWN,
            fee=30,
            principal=970,
            reward=0,
            reward_due=0,
            intervals_passed=0,
        )
        return stake_coin.WithdrawOutcome(**{**defaults, **kwargs})

    def test_a_paid_reward_is_announced(self):
        message = stake_coin.withdraw_message(self.outcome(reward=21, reward_due=21, intervals_passed=1))

        assert message == "您已取出质押本金 970 金币（手续费 30 金币），并获得回报 21 金币！"

    def test_a_pool_that_could_not_pay_is_explained(self):
        message = stake_coin.withdraw_message(self.outcome(reward_due=21, intervals_passed=1))

        assert "奖励池余额不足" in message

    def test_a_reward_below_one_coin_is_explained(self):
        message = stake_coin.withdraw_message(self.outcome(intervals_passed=1))

        assert "累计回报不足 1 金币" in message

    def test_an_early_withdrawal_is_explained(self):
        message = stake_coin.withdraw_message(self.outcome())

        assert "未满7天" in message


class TestOpKeys:
    STAKE_TIME = datetime(2026, 10, 5, 8, 30, 15)
    LATER = datetime(2026, 10, 12, 8, 30, 15)

    def test_every_derived_key_is_a_valid_op_key_even_with_large_ids(self):
        user_id = 7_000_000_000_000
        chat_id = -1_001_870_858_408
        message_id = 2_147_483_647
        keys = [
            shop.shop_op_key("scratch", "9223372036854775807"),
            stake_coin.stake_open_op_key(chat_id, message_id),
            stake_coin.stake_collect_op_key(user_id, self.STAKE_TIME, self.LATER),
            stake_coin.stake_withdraw_op_key(user_id, self.STAKE_TIME),
            stake_coin.stake_withdraw_reward_op_key(user_id, self.STAKE_TIME),
            coins.give_op_key(chat_id, message_id),
            coins.give_recipient_op_key(chat_id, message_id),
            ref.invitee_op_key(user_id),
            ref.referrer_op_key(user_id),
            task.task_op_key(user_id, 2),
            crypto_predict.prediction_op_key(user_id, self.STAKE_TIME, "expired"),
            swap.swap_op_key(chat_id, message_id),
            bribe.bribe_op_key(chat_id, message_id),
            user_tools.kindness_op_key(user_id, self.STAKE_TIME),
        ]

        for key in keys:
            assert balance.check_op_key(key) == key
        assert len(set(keys)) == len(keys)

    def test_keys_are_deterministic_functions_of_the_durable_identity(self):
        assert coins.give_op_key(9, 11) == coins.give_op_key(9, 11) == "give:9:11"
        assert coins.give_op_key(9, 11) != coins.give_op_key(9, 12)
        assert coins.give_recipient_op_key(9, 11) == "give:9:11:recv"
        assert swap.swap_op_key(9, 11) == "swap:9:11"
        assert task.task_op_key(5, 1) == "task:5:1"
        assert ref.invitee_op_key(5) == "ref_invitee:5"
        assert ref.referrer_op_key(5) == "ref_referrer:5"

    def test_shop_keys_are_per_click_and_per_item(self):
        assert shop.shop_op_key("memory", "q1") == "shop:memory:q1"
        assert shop.shop_op_key("memory", "q1") != shop.shop_op_key("memory", "q2")
        assert shop.shop_op_key("memory", "q1") != shop.shop_op_key("perm1", "q1")

    def test_a_missing_query_id_falls_back_to_a_one_off_key(self):
        first = shop.shop_op_key("memory", None)
        second = shop.shop_op_key("memory", None)

        assert first != second
        assert first.startswith("shop:memory:")

    def test_collect_keys_identify_the_stake_and_the_reward_window(self):
        first_window = stake_coin.stake_collect_op_key(5, self.STAKE_TIME, self.STAKE_TIME)
        second_window = stake_coin.stake_collect_op_key(5, self.STAKE_TIME, self.LATER)

        assert first_window == "stake_collect:5:20261005T083015:20261005T083015"
        assert second_window != first_window
        assert stake_coin.stake_withdraw_op_key(5, self.STAKE_TIME) == (
            "stake_withdraw:5:20261005T083015"
        )

    def test_prediction_keys_separate_the_bet_the_win_and_the_expiry_refund(self):
        keys = {
            step: crypto_predict.prediction_op_key(5, self.STAKE_TIME, step)
            for step in ("bet", "win", "expired")
        }

        assert keys["bet"] == "btc:5:20261005T083015:bet"
        assert len(set(keys.values())) == 3

    def test_kindness_key_follows_the_previous_gift(self):
        assert user_tools.kindness_op_key(5, None) == "kindness:5:never"
        assert user_tools.kindness_op_key(5, self.STAKE_TIME) == "kindness:5:20261005T083015"
        assert user_tools.kindness_op_key(5, self.STAKE_TIME) != user_tools.kindness_op_key(
            5, self.LATER
        )
