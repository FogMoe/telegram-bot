"""商店购买：扣款与发放在同一个事务里，重复点击与并发不双花，失败整体回滚（真实 MySQL）。"""

import asyncio
from datetime import date
from types import SimpleNamespace

import pytest
from economy_support import (
    Recorder,
    gather_all,
    ledger_keys,
    ledger_rows,
    make_callback_update,
    make_context,
    seed_user,
    user_state,
)
from mysql_support import execute, fetch_scalar, run

from core import balance
from features.economy import shop, shop_views
from features.economy.operations import shop as shop_purchases
from features.economy.operations.shop import (
    MemoryLimitPurchase,
    PermissionUpgrade,
    PurchaseStatus,
    TicketPurchase,
)

TODAY = date(2026, 10, 5)


@pytest.fixture(autouse=True)
def fresh_shop_state(monkeypatch):
    # 进程内状态：锁绑定事件循环，保底记录跨测试会互相影响，每个测试换新的。
    monkeypatch.setattr(shop, "lock", asyncio.Lock())
    monkeypatch.setattr(shop_purchases, "scratch_records", {})
    monkeypatch.setattr(shop_purchases, "huanle_records", {})
    monkeypatch.setattr(shop, "last_lottery_messages", {})


def memory_limit(url, user_id=1):
    return fetch_scalar(url, "SELECT permanent_records_limit FROM `user` WHERE id = %s", (user_id,))


def permission(url, user_id=1):
    return fetch_scalar(url, "SELECT permission FROM `user` WHERE id = %s", (user_id,))


async def buy_memory_limit(user_id, op_key):
    """购买并返回给用户看的文案（操作结果经展示层转成文案）。"""
    result = await shop_purchases.buy_memory_limit(MemoryLimitPurchase(user_id, op_key))
    return shop_views.memory_limit_message(result)


async def upgrade_permission(user_id, level, op_key):
    result = await shop_purchases.upgrade_permission(PermissionUpgrade(user_id, level, op_key))
    return shop_views.permission_message(result)


def fail_after(monkeypatch, name):
    """让 balance.<name> 先正常执行、再抛错：模拟事务里这一步之后的任何失败。"""
    real = getattr(balance, name)

    async def wrapper(*args, **kwargs):
        await real(*args, **kwargs)
        raise RuntimeError("injected failure")

    monkeypatch.setattr(balance, name, wrapper)


class TestTypedResults:
    """购买操作返回类型化的结果，文案由展示层从结果生成。"""

    def test_a_memory_purchase_reports_the_status_and_the_new_limit(self, app_database):
        seed_user(app_database, 1, free=250)

        first = run(shop_purchases.buy_memory_limit(MemoryLimitPurchase(1, "shop:memory:q1")))
        replay = run(shop_purchases.buy_memory_limit(MemoryLimitPurchase(1, "shop:memory:q1")))

        assert first == shop_purchases.MemoryLimitResult(PurchaseStatus.PURCHASED, 101)
        assert replay == first

    def test_declined_memory_purchases_carry_only_a_status(self, app_database):
        seed_user(app_database, 1, free=10)

        poor = run(shop_purchases.buy_memory_limit(MemoryLimitPurchase(1, "shop:memory:q1")))
        stranger = run(shop_purchases.buy_memory_limit(MemoryLimitPurchase(404, "shop:memory:q2")))

        assert poor == shop_purchases.MemoryLimitResult(PurchaseStatus.INSUFFICIENT)
        assert stranger == shop_purchases.MemoryLimitResult(PurchaseStatus.NOT_REGISTERED)

    def test_a_permission_purchase_reports_the_new_level(self, app_database):
        seed_user(app_database, 1, free=500)

        result = run(
            shop_purchases.upgrade_permission(PermissionUpgrade(1, 1, "shop:perm1:q1"))
        )

        assert result == shop_purchases.PermissionUpgradeResult(PurchaseStatus.PURCHASED, level=1)

    def test_a_refused_upgrade_names_the_reason(self, app_database):
        seed_user(app_database, 1, free=500)

        result = run(
            shop_purchases.upgrade_permission(PermissionUpgrade(1, 3, "shop:perm3:q1"))
        )

        assert result.status is PurchaseStatus.NOT_ELIGIBLE
        assert result.refusal is shop_purchases.UpgradeRefusal.NEED_LEVEL_2

    def test_a_ticket_result_carries_reward_bonus_and_the_pity_to_commit(self, app_database):
        seed_user(app_database, 1, free=30)

        result = run(
            shop_purchases.buy_huanle_ticket(TicketPurchase(1, "shop:huanle:q1"), today=TODAY)
        )

        assert result.status is PurchaseStatus.PURCHASED
        assert result.reward >= 0 and result.bonus == 0
        miss = result.reward == 0
        assert result.pity == {"count": 1 if miss else 0, "date": TODAY}

    def test_a_declined_ticket_reports_the_balance_it_saw(self, app_database):
        seed_user(app_database, 1, free=4)

        result = run(
            shop_purchases.buy_scratch_ticket(TicketPurchase(1, "shop:scratch:q1"), today=TODAY)
        )

        assert result == shop_purchases.TicketResult(PurchaseStatus.INSUFFICIENT, balance_total=4)


class TestMemoryLimit:
    def test_purchase_debits_and_raises_the_limit_in_one_step(self, app_database):
        seed_user(app_database, 1, free=60, paid=60)

        message = run(buy_memory_limit(1, "shop:memory:q1"))

        assert message == "购买成功！永久记忆上限已提升至 101 条。"
        assert user_state(app_database, 1)["free"] + user_state(app_database, 1)["paid"] == 20
        assert memory_limit(app_database) == 101
        rows = ledger_rows(app_database)
        assert [(row["op_key"], row["kind"], row["reason"]) for row in rows] == [
            ("shop:memory:q1", "debit", "shop_memory")
        ]
        # 先扣免费再扣付费，沿用余额服务的拆分。
        assert (rows[0]["delta_free"], rows[0]["delta_paid"]) == (-60, -40)

    def test_an_insufficient_balance_changes_nothing(self, app_database):
        seed_user(app_database, 1, free=99)

        message = run(buy_memory_limit(1, "shop:memory:q1"))

        assert message == shop_views.INSUFFICIENT_MESSAGE
        assert memory_limit(app_database) == 100
        assert user_state(app_database, 1)["free"] == 99
        assert ledger_rows(app_database) == []

    def test_an_unregistered_user_is_told_to_register(self, app_database):
        message = run(buy_memory_limit(404, "shop:memory:q1"))

        assert message == shop_views.NOT_REGISTERED_MESSAGE
        assert ledger_rows(app_database) == []

    def test_the_same_click_delivered_twice_is_charged_and_applied_once(self, app_database):
        seed_user(app_database, 1, free=250)

        first = run(buy_memory_limit(1, "shop:memory:q1"))
        second = run(buy_memory_limit(1, "shop:memory:q1"))

        assert first == second
        assert memory_limit(app_database) == 101
        assert user_state(app_database, 1)["free"] == 150
        assert ledger_keys(app_database) == ["shop:memory:q1"]

    def test_concurrent_clicks_cannot_spend_the_same_coins_twice(self, app_database):
        seed_user(app_database, 1, free=150)

        async def scenario():
            # 直接调用购买函数，绕过进程内的 asyncio 锁，验证数据库层的串行。
            return await gather_all(
                *[buy_memory_limit(1, f"shop:memory:q{index}") for index in range(5)]
            )

        results = run(scenario())

        assert all(not isinstance(item, Exception) for item in results), results
        assert sorted(results).count(shop_views.INSUFFICIENT_MESSAGE) == 4
        assert memory_limit(app_database) == 101
        assert user_state(app_database, 1)["free"] == 50
        assert len(ledger_rows(app_database)) == 1

    def test_a_failure_after_the_debit_rolls_the_purchase_back(self, app_database, monkeypatch):
        seed_user(app_database, 1, free=150)
        fail_after(monkeypatch, "debit")

        with pytest.raises(RuntimeError):
            run(buy_memory_limit(1, "shop:memory:q1"))

        assert user_state(app_database, 1)["free"] == 150
        assert memory_limit(app_database) == 100
        assert ledger_rows(app_database) == []


class TestPermissionUpgrade:
    def test_level_one_costs_50_and_sets_the_permission(self, app_database):
        seed_user(app_database, 1, free=80)

        message = run(upgrade_permission(1, 1, "shop:perm1:q1"))

        assert message == "购买成功！您的权限已升级到1级。"
        assert permission(app_database) == 1
        assert user_state(app_database, 1)["free"] == 30
        assert ledger_keys(app_database) == ["shop:perm1:q1"]

    def test_levels_must_be_bought_in_order(self, app_database):
        seed_user(app_database, 1, free=20000)

        skipped_two = run(upgrade_permission(1, 2, "shop:perm2:q1"))
        skipped_three = run(upgrade_permission(1, 3, "shop:perm3:q2"))

        assert skipped_two == "您需要先升级到1级权限。"
        assert skipped_three == "您需要先升级到2级权限。"
        assert permission(app_database) == 0
        assert ledger_rows(app_database) == []

    def test_each_level_is_charged_its_own_price(self, app_database):
        seed_user(app_database, 1, free=10200)

        for level, price in ((1, 50), (2, 100), (3, 10000)):
            run(upgrade_permission(1, level, f"shop:perm{level}:q{level}"))
            assert permission(app_database) == level
            assert -ledger_rows(app_database)[-1]["delta_free"] == price

        assert user_state(app_database, 1)["free"] == 50

    def test_a_level_that_is_already_owned_is_refused_without_charging(self, app_database):
        seed_user(app_database, 1, free=500)
        execute(app_database, "UPDATE `user` SET permission = 2 WHERE id = 1")

        again_one = run(upgrade_permission(1, 1, "shop:perm1:q1"))
        again_two = run(upgrade_permission(1, 2, "shop:perm2:q2"))

        assert again_one == "您已经拥有权限或已升级。"
        assert again_two == "您已经拥有2级或更高权限。"
        assert user_state(app_database, 1)["free"] == 500
        assert ledger_rows(app_database) == []

    def test_an_insufficient_balance_keeps_the_old_permission(self, app_database):
        seed_user(app_database, 1, free=49)

        message = run(upgrade_permission(1, 1, "shop:perm1:q1"))

        assert message == shop_views.INSUFFICIENT_MESSAGE
        assert permission(app_database) == 0
        assert ledger_rows(app_database) == []

    def test_concurrent_upgrades_to_the_same_level_charge_once(self, app_database):
        seed_user(app_database, 1, free=500)

        async def scenario():
            return await gather_all(
                *[upgrade_permission(1, 1, f"shop:perm1:q{index}") for index in range(4)]
            )

        results = run(scenario())

        assert all(not isinstance(item, Exception) for item in results), results
        assert results.count("购买成功！您的权限已升级到1级。") == 1
        assert permission(app_database) == 1
        assert user_state(app_database, 1)["free"] == 450
        assert len(ledger_rows(app_database)) == 1

    def test_a_failure_after_the_debit_leaves_permission_and_balance_untouched(
        self, app_database, monkeypatch
    ):
        seed_user(app_database, 1, free=100)
        fail_after(monkeypatch, "debit")

        with pytest.raises(RuntimeError):
            run(upgrade_permission(1, 1, "shop:perm1:q1"))

        assert permission(app_database) == 0
        assert user_state(app_database, 1)["free"] == 100
        assert ledger_rows(app_database) == []


class TestScratchTicket:
    @pytest.fixture
    def reward(self, monkeypatch):
        """固定刮刮乐的开奖结果。"""
        holder = SimpleNamespace(value=7)
        monkeypatch.setattr(shop_purchases, "draw_scratch_reward", lambda: holder.value)
        return holder

    def buy(self, key="shop:scratch:q1"):
        return run(shop_purchases.buy_scratch_ticket(TicketPurchase(1, key), today=TODAY))

    def test_a_ticket_debits_the_price_and_credits_the_reward(self, app_database, reward):
        seed_user(app_database, 1, free=30)
        reward.value = 14

        purchase = self.buy()

        assert (purchase.status, purchase.reward, purchase.bonus) == (PurchaseStatus.PURCHASED, 14, 0)
        assert user_state(app_database, 1)["free"] == 34
        assert [(row["op_key"], row["kind"]) for row in ledger_rows(app_database)] == [
            ("shop:scratch:q1", "debit"),
            ("shop:scratch:q1:win", "credit"),
        ]

    def test_a_zero_reward_leaves_only_the_debit(self, app_database, reward):
        seed_user(app_database, 1, free=30)
        reward.value = 0

        self.buy()

        assert user_state(app_database, 1)["free"] == 20
        assert ledger_keys(app_database) == ["shop:scratch:q1"]

    def test_an_insufficient_balance_reports_the_current_balance(self, app_database, reward):
        seed_user(app_database, 1, free=9)

        purchase = self.buy()

        assert purchase.status is PurchaseStatus.INSUFFICIENT
        assert (
            shop_views.ticket_message(purchase, shop_views.SCRATCH_VIEW)
            == "硬币不足，您当前只有 9 个硬币。"
        )
        assert ledger_rows(app_database) == []
        assert shop_purchases.scratch_records == {}

    def test_five_misses_in_a_row_grant_the_consolation_bonus_once(self, app_database, reward):
        seed_user(app_database, 1, free=100)
        reward.value = 3

        purchases = [self.buy(f"shop:scratch:q{index}") for index in range(5)]

        assert [purchase.bonus for purchase in purchases] == [0, 0, 0, 0, 10]
        assert "shop:scratch:q4:bonus" in ledger_keys(app_database)
        assert shop_purchases.scratch_records[1]["count"] == 0
        # 5 次 -10 +3，加上一次保底 +10。
        assert user_state(app_database, 1)["free"] == 100 - 5 * 10 + 5 * 3 + 10

    def test_a_ten_coin_win_resets_the_miss_streak(self, app_database, reward):
        seed_user(app_database, 1, free=100)
        reward.value = 3
        for index in range(3):
            self.buy(f"shop:scratch:q{index}")
        reward.value = 10

        self.buy("shop:scratch:q9")

        assert shop_purchases.scratch_records[1]["count"] == 0

    def test_the_same_click_delivered_twice_pays_out_once(self, app_database, reward):
        seed_user(app_database, 1, free=30)
        reward.value = 14

        first = self.buy()
        reward.value = 20  # 重放不会重新开奖
        replay = self.buy()

        assert replay.reward == first.reward == 14
        assert replay.pity is None
        assert user_state(app_database, 1)["free"] == 34
        assert len(ledger_rows(app_database)) == 2
        assert shop_purchases.scratch_records[1]["count"] == 0

    def test_a_failure_after_the_reward_rolls_back_the_debit_and_the_streak(
        self, app_database, reward, monkeypatch
    ):
        seed_user(app_database, 1, free=30)
        reward.value = 14
        fail_after(monkeypatch, "credit")

        with pytest.raises(RuntimeError):
            self.buy()

        assert user_state(app_database, 1)["free"] == 30
        assert ledger_rows(app_database) == []
        assert shop_purchases.scratch_records == {}

    def test_concurrent_tickets_never_spend_more_than_the_balance(self, app_database, reward):
        seed_user(app_database, 1, free=25)
        reward.value = 0

        async def scenario():
            return await gather_all(
                *[
                    shop_purchases.buy_scratch_ticket(
                        TicketPurchase(1, f"shop:scratch:q{index}"), today=TODAY
                    )
                    for index in range(6)
                ]
            )

        results = run(scenario())

        assert all(not isinstance(item, Exception) for item in results), results
        assert sum(1 for item in results if item.status is PurchaseStatus.PURCHASED) == 2
        assert user_state(app_database, 1)["free"] == 5


class TestHuanleTicket:
    def test_a_ticket_costs_one_coin_and_pays_the_drawn_reward(self, app_database, monkeypatch):
        seed_user(app_database, 1, free=3)
        monkeypatch.setattr(shop_purchases, "draw_huanle_reward", lambda: 5)

        purchase = run(
            shop_purchases.buy_huanle_ticket(TicketPurchase(1, "shop:huanle:q1"), today=TODAY)
        )

        assert (purchase.status, purchase.reward, purchase.bonus) == (PurchaseStatus.PURCHASED, 5, 0)
        assert user_state(app_database, 1)["free"] == 7

    def test_five_empty_tickets_grant_two_coins(self, app_database, monkeypatch):
        seed_user(app_database, 1, free=10)
        monkeypatch.setattr(shop_purchases, "draw_huanle_reward", lambda: 0)

        purchases = [
            run(
                shop_purchases.buy_huanle_ticket(
                    TicketPurchase(1, f"shop:huanle:q{index}"), today=TODAY
                )
            )
            for index in range(5)
        ]

        assert [purchase.bonus for purchase in purchases] == [0, 0, 0, 0, 2]
        assert user_state(app_database, 1)["free"] == 10 - 5 + 2

    def test_an_empty_wallet_cannot_buy(self, app_database, monkeypatch):
        seed_user(app_database, 1, free=0)
        monkeypatch.setattr(shop_purchases, "draw_huanle_reward", lambda: 100)

        purchase = run(
            shop_purchases.buy_huanle_ticket(TicketPurchase(1, "shop:huanle:q1"), today=TODAY)
        )

        assert purchase.status is PurchaseStatus.INSUFFICIENT
        assert (
            shop_views.ticket_message(purchase, shop_views.HUANLE_VIEW)
            == "硬币不足，您当前只有 0 个硬币。"
        )
        assert ledger_rows(app_database) == []


class TestShopCallback:
    def click(self, data, *, query_id="q-1", user_id=1):
        update, answer, _ = make_callback_update(
            from_user_id=user_id, data=data, query_id=query_id
        )
        sent = Recorder(result=SimpleNamespace(message_id=900))
        context = make_context(send_message=sent, edit_message_text=Recorder())
        run(shop.shop_callback(update, context))
        return answer, sent

    def test_buying_a_scratch_ticket_answers_and_posts_the_record(self, app_database, monkeypatch):
        seed_user(app_database, 1, free=30)
        monkeypatch.setattr(shop_purchases, "draw_scratch_reward", lambda: 12)

        answer, sent = self.click("shop_scratch")

        assert answer.texts == ["恭喜！您获得了 12 个金币。"]
        assert "刮刮乐 → 12金币" in sent.texts[0]
        assert user_state(app_database, 1)["free"] == 32

    def test_a_redelivered_click_does_not_charge_again(self, app_database, monkeypatch):
        seed_user(app_database, 1, free=30)
        monkeypatch.setattr(shop_purchases, "draw_scratch_reward", lambda: 12)
        monkeypatch.setattr(shop, "lock", asyncio.Lock())

        self.click("shop_scratch", query_id="same")
        answer, _ = self.click("shop_scratch", query_id="same")

        assert answer.texts == ["恭喜！您获得了 12 个金币。"]
        assert user_state(app_database, 1)["free"] == 32
        assert len(ledger_rows(app_database)) == 2

    def test_a_declined_purchase_is_shown_as_an_alert_and_charges_nothing(self, app_database):
        seed_user(app_database, 1, free=5)

        answer, sent = self.click("shop_upgrade_1")

        assert answer.texts == [shop_views.INSUFFICIENT_MESSAGE]
        assert answer.calls[0][1]["show_alert"] is True
        assert sent.calls == []
        assert ledger_rows(app_database) == []

    def test_a_database_failure_is_reported_without_charging(self, app_database, monkeypatch):
        seed_user(app_database, 1, free=500)
        fail_after(monkeypatch, "debit")

        answer, _ = self.click("shop_buy_memory_limit")

        assert answer.texts == ["购买出现错误，请稍后再试。"]
        assert user_state(app_database, 1)["free"] == 500
        assert ledger_rows(app_database) == []
