"""商店适配层：回调数据到动作的映射、购买请求的组装、结果到弹窗与聊天记录的投递（不连数据库）。

购买规则与事务在 tests/integration/test_shop_purchases.py；这里用替身替换购买操作，只验证输入映射与回复。
"""

import asyncio
from types import SimpleNamespace

import pytest

from features.economy import shop, shop_views
from features.economy.operations import shop as shop_purchases
from features.economy.operations.shop import (
    MemoryLimitPurchase,
    MemoryLimitResult,
    PermissionUpgrade,
    PermissionUpgradeResult,
    PurchaseStatus,
    TicketPurchase,
    TicketResult,
    UpgradeRefusal,
)
from features.economy.shop_views import Action


class Recorder:
    def __init__(self, result=None, error=None):
        self.calls = []
        self._result = result
        self._error = error

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self._error is not None:
            raise self._error
        return self._result

    @property
    def texts(self):
        return [kwargs.get("text") or args[0] for args, kwargs in self.calls]


def make_query(data, *, query_id="q-1", username="kc", first_name="Kc", edit_error=None, delete_error=None):
    return SimpleNamespace(
        id=query_id,
        data=data,
        from_user=SimpleNamespace(id=7, username=username, first_name=first_name),
        answer=Recorder(),
        edit_message_text=Recorder(error=edit_error),
        delete_message=Recorder(error=delete_error),
    )


def make_context():
    bot = SimpleNamespace(
        send_message=Recorder(result=SimpleNamespace(message_id=900)),
        edit_message_text=Recorder(),
    )
    return SimpleNamespace(bot=bot)


def click(query, context=None):
    update = SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=-100))
    context = context or make_context()
    asyncio.run(shop.shop_callback(update, context))
    return context


@pytest.fixture(autouse=True)
def fresh_adapter_state(monkeypatch):
    monkeypatch.setattr(shop, "lock", asyncio.Lock())
    monkeypatch.setattr(shop, "last_lottery_messages", {})


def fake_purchase(monkeypatch, name, result):
    """替换购买操作：记录收到的请求，返回给定结果（异常则抛出）。"""
    requests = []

    async def fake(request, **kwargs):
        requests.append(request)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(shop_purchases, name, fake)
    return requests


class TestCallbackMapping:
    def test_every_menu_button_maps_to_an_action_the_handler_pattern_accepts(self):
        for menu in (shop_views.home_menu(), shop_views.permission_menu(), shop_views.lottery_menu()):
            for row in menu.keyboard.inline_keyboard:
                for button in row:
                    assert button.callback_data.startswith("shop_")
                    assert shop_views.parse_callback(button.callback_data) is not None

    def test_upgrade_buttons_carry_the_target_level(self):
        assert shop_views.parse_callback("shop_upgrade_2") == shop_views.ShopCallback(
            Action.UPGRADE_PERMISSION, 2
        )

    @pytest.mark.parametrize("data", [None, "", "shop_unknown", "shop_upgrade_9", "upgrade_1"])
    def test_unknown_data_is_ignored_without_any_reply(self, data):
        query = make_query(data)

        click(query)

        assert query.answer.calls == []
        assert query.edit_message_text.calls == []
        assert query.delete_message.calls == []

    def test_every_upgrade_button_has_a_price(self):
        assert set(shop_views.UPGRADE_CALLBACKS.values()) == set(
            shop_purchases.PERMISSION_UPGRADE_PRICES
        )


class TestNavigation:
    @pytest.mark.parametrize(
        ("data", "menu"),
        [
            ("shop_buy_permission", shop_views.permission_menu()),
            ("shop_buy_lottery", shop_views.lottery_menu()),
            ("shop_home", shop_views.home_menu()),
        ],
    )
    def test_menu_buttons_edit_the_message_in_place(self, data, menu):
        query = make_query(data)

        click(query)

        ((args, kwargs),) = query.edit_message_text.calls
        assert args == (menu.text,)
        assert kwargs["reply_markup"] == menu.keyboard
        assert query.answer.calls == []

    def test_a_failed_edit_is_swallowed(self):
        query = make_query("shop_home", edit_error=RuntimeError("message is not modified"))

        click(query)

        assert len(query.edit_message_text.calls) == 1

    def test_close_deletes_the_shop_message(self):
        query = make_query("shop_close")

        click(query)

        assert len(query.delete_message.calls) == 1

    def test_a_failed_delete_is_swallowed(self):
        query = make_query("shop_close", delete_error=RuntimeError("too old"))

        click(query)

        assert len(query.delete_message.calls) == 1

    def test_the_command_sends_the_home_menu(self):
        replies = Recorder()
        update = SimpleNamespace(message=SimpleNamespace(reply_text=replies))

        asyncio.run(shop.shop_command.__wrapped__(update, make_context()))

        ((args, kwargs),) = replies.calls
        assert args == (shop_views.home_menu().text,)
        assert kwargs["reply_markup"] == shop_views.home_menu().keyboard


class TestMemoryLimitAndPermission:
    def test_the_request_carries_the_clicking_user_and_the_query_identity(self, monkeypatch):
        requests = fake_purchase(
            monkeypatch,
            "buy_memory_limit",
            MemoryLimitResult(PurchaseStatus.PURCHASED, new_limit=101),
        )
        query = make_query("shop_buy_memory_limit", query_id="abc")

        click(query)

        assert requests == [MemoryLimitPurchase(7, "shop:memory:abc")]
        ((args, kwargs),) = query.answer.calls
        assert args == ("购买成功！永久记忆上限已提升至 101 条。",)
        assert kwargs["show_alert"] is True

    @pytest.mark.parametrize(
        ("result", "text"),
        [
            (MemoryLimitResult(PurchaseStatus.NOT_REGISTERED), shop_views.NOT_REGISTERED_MESSAGE),
            (MemoryLimitResult(PurchaseStatus.INSUFFICIENT), shop_views.INSUFFICIENT_MESSAGE),
        ],
    )
    def test_declined_memory_purchases_are_alerts(self, monkeypatch, result, text):
        fake_purchase(monkeypatch, "buy_memory_limit", result)
        query = make_query("shop_buy_memory_limit")

        click(query)

        assert query.answer.texts == [text]

    def test_an_unexpected_failure_shows_a_generic_alert(self, monkeypatch):
        fake_purchase(monkeypatch, "buy_memory_limit", RuntimeError("db is down"))
        query = make_query("shop_buy_memory_limit")

        click(query)

        assert query.answer.texts == ["购买出现错误，请稍后再试。"]
        assert query.answer.calls[0][1]["show_alert"] is True

    def test_an_upgrade_request_carries_the_level_from_the_button(self, monkeypatch):
        requests = fake_purchase(
            monkeypatch,
            "upgrade_permission",
            PermissionUpgradeResult(PurchaseStatus.PURCHASED, level=2),
        )
        query = make_query("shop_upgrade_2", query_id="abc")

        click(query)

        assert requests == [PermissionUpgrade(7, 2, "shop:perm2:abc")]
        assert query.answer.texts == ["购买成功！您的权限已升级到2级。"]

    @pytest.mark.parametrize(
        ("refusal", "text"),
        [
            (UpgradeRefusal.ALREADY_UPGRADED, "您已经拥有权限或已升级。"),
            (UpgradeRefusal.NEED_LEVEL_1, "您需要先升级到1级权限。"),
            (UpgradeRefusal.HAS_LEVEL_2, "您已经拥有2级或更高权限。"),
            (UpgradeRefusal.NEED_LEVEL_2, "您需要先升级到2级权限。"),
            (UpgradeRefusal.HAS_LEVEL_3, "您已经拥有3级或更高权限。"),
        ],
    )
    def test_each_refusal_reason_has_its_own_text(self, monkeypatch, refusal, text):
        fake_purchase(
            monkeypatch,
            "upgrade_permission",
            PermissionUpgradeResult(PurchaseStatus.NOT_ELIGIBLE, refusal=refusal),
        )
        query = make_query("shop_upgrade_1")

        click(query)

        assert query.answer.texts == [text]

    def test_an_unexpected_upgrade_failure_shows_a_generic_alert(self, monkeypatch):
        fake_purchase(monkeypatch, "upgrade_permission", RuntimeError("db is down"))
        query = make_query("shop_upgrade_3")

        click(query)

        assert query.answer.texts == ["购买出现错误，请稍后再试。"]


class TestLotteryTickets:
    def test_a_win_answers_and_posts_the_record_to_the_chat(self, monkeypatch):
        requests = fake_purchase(
            monkeypatch,
            "buy_scratch_ticket",
            TicketResult(PurchaseStatus.PURCHASED, reward=12),
        )
        query = make_query("shop_scratch", query_id="abc")

        context = click(query)

        assert requests == [TicketPurchase(7, "shop:scratch:abc")]
        assert query.answer.texts == ["恭喜！您获得了 12 个金币。"]
        ((args, kwargs),) = context.bot.send_message.calls
        assert kwargs["chat_id"] == -100
        assert kwargs["text"] == "📊 最近的彩票记录:\n@kc: 刮刮乐 → 12金币"

    def test_the_consolation_bonus_is_explained_and_recorded(self, monkeypatch):
        fake_purchase(
            monkeypatch,
            "buy_huanle_ticket",
            TicketResult(PurchaseStatus.PURCHASED, reward=0, bonus=2),
        )
        query = make_query("shop_huanle")

        context = click(query)

        assert query.answer.texts == [
            "恭喜！您获得了 0 个金币。\n\n由于您连续5次都没有获得奖励，系统赠送您2个金币作为安慰！"
        ]
        assert context.bot.send_message.texts == [
            "📊 最近的彩票记录:\n@kc: 欢乐彩 → 0金币 (触发保底奖励2金币!)"
        ]

    def test_a_user_without_a_username_is_listed_by_first_name(self, monkeypatch):
        fake_purchase(
            monkeypatch,
            "buy_scratch_ticket",
            TicketResult(PurchaseStatus.PURCHASED, reward=3),
        )
        query = make_query("shop_scratch", username=None, first_name="Kc")

        context = click(query)

        assert context.bot.send_message.texts == ["📊 最近的彩票记录:\nKc: 刮刮乐 → 3金币"]

    def test_an_insufficient_balance_is_an_alert_without_a_chat_record(self, monkeypatch):
        fake_purchase(
            monkeypatch,
            "buy_scratch_ticket",
            TicketResult(PurchaseStatus.INSUFFICIENT, balance_total=3),
        )
        query = make_query("shop_scratch")

        context = click(query)

        assert query.answer.texts == ["硬币不足，您当前只有 3 个硬币。"]
        assert query.answer.calls[0][1]["show_alert"] is True
        assert context.bot.send_message.calls == []

    def test_an_unregistered_user_is_told_to_register(self, monkeypatch):
        fake_purchase(
            monkeypatch,
            "buy_huanle_ticket",
            TicketResult(PurchaseStatus.NOT_REGISTERED),
        )
        query = make_query("shop_huanle")

        click(query)

        assert query.answer.texts == [shop_views.NOT_REGISTERED_MESSAGE]

    def test_a_scratch_failure_carries_an_error_reference(self, monkeypatch):
        fake_purchase(monkeypatch, "buy_scratch_ticket", RuntimeError("db is down"))
        query = make_query("shop_scratch")

        click(query)

        (text,) = query.answer.texts
        assert text.startswith("购买刮刮乐时出错，请稍后再试。\n")
        assert "ERR-" in text
        assert "db is down" not in text

    def test_a_huanle_failure_is_a_plain_alert(self, monkeypatch):
        fake_purchase(monkeypatch, "buy_huanle_ticket", RuntimeError("db is down"))
        query = make_query("shop_huanle")

        click(query)

        assert query.answer.texts == ["购买欢乐彩时出错，请稍后再试。"]

    def test_the_purchase_lock_serializes_purchases(self, monkeypatch):
        order = []

        async def slow_purchase(request, **kwargs):
            order.append(("start", request.op_key))
            await asyncio.sleep(0.01)
            order.append(("end", request.op_key))
            return TicketResult(PurchaseStatus.PURCHASED, reward=1)

        monkeypatch.setattr(shop_purchases, "buy_scratch_ticket", slow_purchase)

        async def scenario():
            jobs = []
            for qid in ("a", "b"):
                query = make_query("shop_scratch", query_id=qid)
                update = SimpleNamespace(
                    callback_query=query, effective_chat=SimpleNamespace(id=-100)
                )
                jobs.append(shop.shop_callback(update, make_context()))
            await asyncio.gather(*jobs)

        asyncio.run(scenario())

        assert order == [
            ("start", "shop:scratch:a"),
            ("end", "shop:scratch:a"),
            ("start", "shop:scratch:b"),
            ("end", "shop:scratch:b"),
        ]


class TestLotteryFeed:
    PURCHASED = TicketResult(PurchaseStatus.PURCHASED, reward=5)

    @pytest.fixture
    def clock(self, monkeypatch):
        now = SimpleNamespace(value=1000.0)
        monkeypatch.setattr(shop.time, "time", lambda: now.value)
        return now

    def buy(self, monkeypatch, context):
        fake_purchase(monkeypatch, "buy_scratch_ticket", self.PURCHASED)
        click(make_query("shop_scratch"), context)

    def test_purchases_within_the_window_extend_one_message(self, monkeypatch, clock):
        context = make_context()

        self.buy(monkeypatch, context)
        clock.value += 10
        self.buy(monkeypatch, context)

        assert len(context.bot.send_message.calls) == 1
        ((args, kwargs),) = context.bot.edit_message_text.calls
        assert kwargs["message_id"] == 900
        assert kwargs["text"] == (
            "📊 最近的彩票记录:\n@kc: 刮刮乐 → 5金币\n@kc: 刮刮乐 → 5金币"
        )

    def test_a_purchase_after_the_window_starts_a_new_message(self, monkeypatch, clock):
        context = make_context()

        self.buy(monkeypatch, context)
        clock.value += shop.MESSAGE_UPDATE_THRESHOLD + 1
        self.buy(monkeypatch, context)

        assert len(context.bot.send_message.calls) == 2
        assert context.bot.edit_message_text.calls == []

    def test_a_full_message_is_not_extended_any_further(self, monkeypatch, clock):
        context = make_context()

        for _ in range(7):
            self.buy(monkeypatch, context)
            clock.value += 1

        # 标题加五行记录满 6 行之后，下一次换一条新消息。
        assert len(context.bot.send_message.calls) == 2
        assert len(context.bot.edit_message_text.calls) == 5

    def test_a_failed_edit_falls_back_to_a_new_message(self, monkeypatch, clock):
        context = make_context()
        context.bot.edit_message_text = Recorder(error=RuntimeError("message was deleted"))

        self.buy(monkeypatch, context)
        clock.value += 1
        self.buy(monkeypatch, context)

        assert len(context.bot.send_message.calls) == 2

    def test_expired_records_are_cleaned_up(self, monkeypatch, clock):
        context = make_context()
        self.buy(monkeypatch, context)
        assert shop.last_lottery_messages
        clock.value += 3601

        asyncio.run(shop.cleanup_message_records_job(None))

        assert shop.last_lottery_messages == {}
