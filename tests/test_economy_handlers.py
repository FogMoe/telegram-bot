"""经济命令的适配层：把业务操作的结果映射成回复，把 Telegram 输入映射成操作参数（不连数据库）。

规则、事务与幂等在各自的业务操作里，由 tests/integration 的 MySQL 测试覆盖；这里用替身替换操作。
"""

import asyncio
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from fogmoe_telegram_bot.core import user_records
from fogmoe_telegram_bot.features.economy import (
    bribe,
    charge_coin,
    checkin,
    coins,
    ref,
    stake_coin,
    task,
    web_password,
)
from fogmoe_telegram_bot.features.economy.operations import bribe as bribe_operations
from fogmoe_telegram_bot.features.economy.operations import charge as charge_operations
from fogmoe_telegram_bot.features.economy.operations import checkin as checkin_operations
from fogmoe_telegram_bot.features.economy.operations import coins as coin_operations
from fogmoe_telegram_bot.features.economy.operations import invitations as invitation_operations
from fogmoe_telegram_bot.features.economy.operations import lottery as lottery_operations
from fogmoe_telegram_bot.features.economy.operations import stake as stake_operations
from fogmoe_telegram_bot.features.economy.operations import task as task_operations
from fogmoe_telegram_bot.features.economy.operations import web_password as web_password_operations
from fogmoe_telegram_bot.features.economy.repositories.coins import RichEntry
from fogmoe_telegram_bot.features.economy.repositories.invitations import InvitedUser, Referrer
from fogmoe_telegram_bot.features.economy.repositories.web_passwords import WebPasswordRecord


def raw(handler):
    """去掉 cooldown / 私聊限制等装饰器，直接调用 handler 本体。"""
    while hasattr(handler, "__wrapped__"):
        handler = handler.__wrapped__
    return handler


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


def command_update(*, user_id=7, username="kc", chat_id=-100, message_id=11, full_name="Kc Fog"):
    reply = Recorder(result=SimpleNamespace(edit_text=Recorder()))
    message = SimpleNamespace(message_id=message_id, reply_text=reply)
    return SimpleNamespace(
        message=message,
        effective_user=SimpleNamespace(
            id=user_id, username=username, first_name="Kc", full_name=full_name
        ),
        effective_chat=SimpleNamespace(id=chat_id, type="private"),
    )


def command_context(*args):
    return SimpleNamespace(
        args=list(args),
        bot=SimpleNamespace(
            send_message=Recorder(),
            get_me=Recorder(result=SimpleNamespace(username="fogmoe_bot")),
            get_chat=Recorder(result=SimpleNamespace(username="boss")),
            get_chat_member=Recorder(result=SimpleNamespace(status="member")),
        ),
    )


def callback_update(data, *, user_id=7):
    query = SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id),
        answer=Recorder(),
        edit_message_text=Recorder(),
        delete_message=Recorder(),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=query.from_user,
        effective_chat=SimpleNamespace(id=user_id),
    )
    return update, query


def fake(monkeypatch, module, name, result):
    """用异步替身替换模块里的函数：记录调用，返回结果（异常则抛出）。"""
    calls = []

    async def replacement(*args, **kwargs):
        calls.append((args, kwargs))
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(module, name, replacement)
    return calls


def run(coro):
    return asyncio.run(coro)


class TestCheckin:
    def run_command(self, monkeypatch, outcome, *, registered=True, username="kc"):
        fake(monkeypatch, user_records, "async_check_user_exists", registered)
        calls = fake(monkeypatch, checkin_operations, "process_checkin", outcome)
        update = command_update(username=username)
        run(raw(checkin.checkin_command)(update, command_context()))
        return update, calls

    def test_a_first_checkin_shows_the_reward_and_the_streak(self, monkeypatch):
        outcome = checkin_operations.CheckinOutcome(
            checkin_operations.CheckinStatus.CHECKED_IN, consecutive_days=3, reward=1
        )

        update, calls = self.run_command(monkeypatch, outcome)

        ((args, kwargs),) = update.message.reply_text.calls
        assert "签到成功" in args[0]
        assert "连续签到: <b>3</b> 天" in args[0]
        assert "今日奖励: <b>1</b> 金币" in args[0]
        assert "距离最高奖励还有 28 天" in args[0]
        assert calls == [((7,), {})]

    def test_reaching_the_top_tier_says_so(self, monkeypatch):
        outcome = checkin_operations.CheckinOutcome(
            checkin_operations.CheckinStatus.CHECKED_IN,
            consecutive_days=checkin_operations.MAX_REWARD_DAYS,
            reward=7,
        )

        update, _ = self.run_command(monkeypatch, outcome)

        assert "已达到最高奖励等级" in update.message.reply_text.texts[0]

    def test_a_repeat_checkin_is_refused_with_the_current_streak(self, monkeypatch):
        outcome = checkin_operations.CheckinOutcome(
            checkin_operations.CheckinStatus.ALREADY_CHECKED_IN, consecutive_days=4
        )

        update, _ = self.run_command(monkeypatch, outcome)

        text = update.message.reply_text.texts[0]
        assert "您今天已经签到过了" in text
        assert "当前连续签到: <b>4</b> 天" in text

    def test_a_username_is_required(self, monkeypatch):
        update, calls = self.run_command(monkeypatch, None, username=None)

        assert "需要设置Telegram用户名" in update.message.reply_text.texts[0]
        assert calls == []

    def test_unregistered_users_are_sent_to_register(self, monkeypatch):
        update, calls = self.run_command(monkeypatch, None, registered=False)

        assert "/me" in update.message.reply_text.texts[0]
        assert calls == []


class TestLotteryAndRich:
    @pytest.mark.parametrize(
        ("outcome", "fragment"),
        [
            (lottery_operations.LotteryOutcome(lottery_operations.LotteryStatus.WON, 7), "7 枚硬币"),
            (lottery_operations.LotteryOutcome(lottery_operations.LotteryStatus.NOT_REGISTERED), "/me"),
            (lottery_operations.LotteryOutcome(lottery_operations.LotteryStatus.COOLING_DOWN), "24小时"),
            (lottery_operations.LotteryOutcome(lottery_operations.LotteryStatus.BUSY), "过于频繁"),
        ],
    )
    def test_every_lottery_status_has_a_message(self, monkeypatch, outcome, fragment):
        calls = fake(monkeypatch, lottery_operations, "async_lottery", outcome)
        update = command_update(chat_id=-55)
        context = command_context()

        run(raw(coins.lottery_command)(update, context))

        assert calls == [((7,), {})]
        ((args, kwargs),) = context.bot.send_message.calls
        assert kwargs["chat_id"] == -55
        assert fragment in kwargs["text"]

    def test_the_leaderboard_lists_the_richest_users_in_order(self, monkeypatch):
        monkeypatch.setattr(coins, "last_rich_query_time", 0)
        calls = fake(
            monkeypatch,
            coin_operations,
            "richest_users",
            [RichEntry("alice", 900), RichEntry("bob", 20)],
        )
        update = command_update()

        run(raw(coins.rich_command)(update, command_context()))

        assert calls == [((5,), {})]
        text = update.message.reply_text.texts[0]
        assert "1. alice - 900 枚硬币" in text and "2. bob - 20 枚硬币" in text

    def test_an_empty_leaderboard_says_so(self, monkeypatch):
        monkeypatch.setattr(coins, "last_rich_query_time", 0)
        fake(monkeypatch, coin_operations, "richest_users", [])
        update = command_update()

        run(raw(coins.rich_command)(update, command_context()))

        assert update.message.reply_text.texts == ["暂无数据"]

    def test_the_leaderboard_is_rate_limited_per_process(self, monkeypatch):
        monkeypatch.setattr(coins, "last_rich_query_time", 0)
        calls = fake(monkeypatch, coin_operations, "richest_users", [RichEntry("alice", 1)])

        first, second = command_update(), command_update()
        run(raw(coins.rich_command)(first, command_context()))
        run(raw(coins.rich_command)(second, command_context()))

        assert len(calls) == 1
        assert "查询过于频繁" in second.message.reply_text.texts[0]

    def test_a_failing_query_gets_a_safe_error_with_a_reference(self, monkeypatch):
        monkeypatch.setattr(coins, "last_rich_query_time", 0)
        fake(monkeypatch, coin_operations, "richest_users", RuntimeError("db is down"))
        update = command_update()

        run(raw(coins.rich_command)(update, command_context()))

        text = update.message.reply_text.texts[0]
        assert "查询富豪榜时出错" in text and "ERR-" in text and "db is down" not in text


class TestGive:
    def give(self, monkeypatch, outcome, *args, recipient=9):
        fake(monkeypatch, coin_operations, "find_recipient_id", recipient)
        calls = fake(monkeypatch, coin_operations, "transfer_coins", outcome)
        update = command_update(chat_id=-100, message_id=11)
        run(raw(coins.give_command)(update, command_context(*args)))
        return update, calls

    def test_the_transfer_request_carries_ids_amount_and_the_command_identity(self, monkeypatch):
        outcome = coin_operations.GiveOutcome(coin_operations.GiveStatus.GIVEN)

        update, calls = self.give(monkeypatch, outcome, "bob", "10")

        ((args, kwargs),) = calls
        assert args == (7, 9, 10)
        assert kwargs["sender_op_key"] == "give:-100:11"
        assert kwargs["recipient_op_key"] == "give:-100:11:recv"
        assert isinstance(kwargs["today"], date)
        assert update.message.reply_text.texts == ["成功赠送 10 枚硬币给用户 bob，手续费 2 枚硬币。"]

    def test_a_one_coin_gift_has_no_fee(self, monkeypatch):
        outcome = coin_operations.GiveOutcome(coin_operations.GiveStatus.GIVEN)

        update, _ = self.give(monkeypatch, outcome, "bob", "1")

        assert update.message.reply_text.texts == ["成功赠送 1 枚硬币给用户 bob。"]

    @pytest.mark.parametrize(
        ("outcome", "fragment"),
        [
            (coin_operations.GiveOutcome(coin_operations.GiveStatus.NOT_REGISTERED), "/me"),
            (
                coin_operations.GiveOutcome(coin_operations.GiveStatus.INSUFFICIENT, balance_total=3),
                "当前硬币：3，需要：12",
            ),
            (coin_operations.GiveOutcome(coin_operations.GiveStatus.DAILY_LIMIT), "已达上限"),
            (coin_operations.GiveOutcome(coin_operations.GiveStatus.RECIPIENT_NOT_FOUND), "未找到用户名为 'bob'"),
            (coin_operations.GiveOutcome(coin_operations.GiveStatus.SELF), "不能给自己赠送"),
        ],
    )
    def test_refusals_are_explained(self, monkeypatch, outcome, fragment):
        update, _ = self.give(monkeypatch, outcome, "bob", "10")

        assert fragment in update.message.reply_text.texts[0]

    @pytest.mark.parametrize("args", [(), ("bob",), ("bob", "10", "x")])
    def test_the_wrong_number_of_arguments_shows_the_usage(self, monkeypatch, args):
        calls = fake(monkeypatch, coin_operations, "transfer_coins", None)
        update = command_update()

        run(raw(coins.give_command)(update, command_context(*args)))

        assert "用法：/give" in update.message.reply_text.texts[0]
        assert calls == []

    @pytest.mark.parametrize("amount", ["0", "-5", "abc", "1.5"])
    def test_the_amount_must_be_a_positive_integer(self, monkeypatch, amount):
        calls = fake(monkeypatch, coin_operations, "transfer_coins", None)
        update = command_update()

        run(raw(coins.give_command)(update, command_context("bob", amount)))

        assert update.message.reply_text.texts == ["赠送数量必须为正整数！"]
        assert calls == []

    def test_an_unexpected_failure_is_a_generic_reply(self, monkeypatch):
        fake(monkeypatch, coin_operations, "find_recipient_id", 9)
        fake(monkeypatch, coin_operations, "transfer_coins", RuntimeError("db is down"))
        update = command_update()

        run(raw(coins.give_command)(update, command_context("bob", "5")))

        assert update.message.reply_text.texts == ["转账过程中出现错误，请稍后再试。"]


class TestTask:
    def claim(self, monkeypatch, data, *, done=False, member_status="member", claimed=None):
        fake(monkeypatch, task_operations, "is_task_completed", done)
        calls = fake(
            monkeypatch,
            task_operations,
            "claim_task_reward",
            claimed or task_operations.TaskClaim.CLAIMED,
        )
        update, query = callback_update(data)
        context = command_context()
        context.bot.get_chat_member = Recorder(result=SimpleNamespace(status=member_status))
        run(task.task_callback(update, context))
        return query, calls, context

    def test_a_member_claims_the_reward_for_the_right_task(self, monkeypatch):
        query, calls, context = self.claim(monkeypatch, "task_check_group2")

        assert calls == [((7, task_operations.TASK_ID_CHECK_GROUP2, task_operations.REWARD_COINS_2), {})]
        ((args, kwargs),) = context.bot.get_chat_member.calls
        assert kwargs == {"chat_id": task_operations.TARGET_GROUP_ID2, "user_id": 7}
        assert query.answer.texts == ["恭喜您完成任务，获得 10 个硬币奖励！"]

    @pytest.mark.parametrize("status", ["left", "kicked"])
    def test_someone_outside_the_group_is_asked_to_join_first(self, monkeypatch, status):
        query, calls, _ = self.claim(monkeypatch, "task_check_group1", member_status=status)

        assert "尚未加入 @ScarletKc_Group" in query.answer.texts[0]
        assert calls == []

    def test_a_completed_task_is_not_checked_against_the_group_again(self, monkeypatch):
        query, calls, context = self.claim(monkeypatch, "task_check_group1", done=True)

        assert query.answer.texts == ["您已完成该任务，不能重复领取奖励。"]
        assert calls == [] and context.bot.get_chat_member.calls == []

    def test_a_failed_membership_lookup_is_reported(self, monkeypatch):
        fake(monkeypatch, task_operations, "is_task_completed", False)
        claim_calls = fake(monkeypatch, task_operations, "claim_task_reward", None)
        update, query = callback_update("task_check_group1")
        context = command_context()
        context.bot.get_chat_member = Recorder(error=RuntimeError("chat not found"))

        run(task.task_callback(update, context))

        assert query.answer.texts == ["无法验证您是否在指定群组，请稍后再试。"]
        assert claim_calls == []

    @pytest.mark.parametrize(
        ("status", "fragment"),
        [
            (task_operations.TaskClaim.NOT_REGISTERED, "/me"),
            (task_operations.TaskClaim.ALREADY_DONE, "已完成该任务"),
        ],
    )
    def test_other_claim_results_are_explained(self, monkeypatch, status, fragment):
        query, _, _ = self.claim(monkeypatch, "task_check_group1", claimed=status)

        assert fragment in query.answer.texts[0]

    def test_a_claim_failure_is_a_generic_alert(self, monkeypatch):
        fake(monkeypatch, task_operations, "is_task_completed", False)
        fake(monkeypatch, task_operations, "claim_task_reward", RuntimeError("db is down"))
        update, query = callback_update("task_check_group1")

        run(task.task_callback(update, command_context()))

        assert query.answer.texts == ["发放奖励时出现错误，请稍后再试。"]

    def test_the_close_button_deletes_the_message(self, monkeypatch):
        update, query = callback_update("task_close")

        run(task.task_callback(update, command_context()))

        assert len(query.delete_message.calls) == 1

    def test_unknown_buttons_are_ignored(self, monkeypatch):
        update, query = callback_update("task_other")

        run(task.task_callback(update, command_context()))

        assert query.answer.calls == [] and query.delete_message.calls == []


class TestStake:
    def callback(self, monkeypatch, action, operation, outcome, *, owner=7, clicker=7):
        calls = fake(monkeypatch, stake_operations, operation, outcome)
        fake(monkeypatch, stake_operations, "calculate_reward_rate", 0.25)
        update, query = callback_update(f"stake_{action}_{owner}", user_id=clicker)
        run(stake_coin.stake_callback(update, command_context()))
        return query, calls

    def test_only_the_owner_can_use_the_stake_buttons(self, monkeypatch):
        query, calls = self.callback(
            monkeypatch,
            "collect",
            "collect_stake_reward",
            None,
            owner=7,
            clicker=8,
        )

        assert query.answer.texts == ["这不是你的质押，你不能操作。"]
        assert calls == []

    @pytest.mark.parametrize(
        ("status", "fragment"),
        [
            (stake_operations.CollectStatus.NO_STAKE, "没有质押任何金币"),
            (stake_operations.CollectStatus.NOT_YET, "需要等待至少7天"),
            (stake_operations.CollectStatus.TOO_SMALL, "累计回报不足 1 金币"),
            (stake_operations.CollectStatus.POOL_EMPTY, "奖励池余额不足"),
        ],
    )
    def test_collect_refusals_are_explained_as_alerts(self, monkeypatch, status, fragment):
        query, _ = self.callback(
            monkeypatch,
            "collect",
            "collect_stake_reward",
            stake_operations.CollectOutcome(status),
        )

        assert fragment in query.answer.texts[0]
        assert query.edit_message_text.calls == []

    def test_a_collected_reward_updates_the_panel_and_confirms(self, monkeypatch):
        query, calls = self.callback(
            monkeypatch,
            "collect",
            "collect_stake_reward",
            stake_operations.CollectOutcome(
                stake_operations.CollectStatus.COLLECTED, reward=21, stake_amount=1000
            ),
        )

        assert calls == [((7,), {})]
        (text,) = query.edit_message_text.texts
        assert "21 金币的回报" in text and "当前质押金额: 1000 金币" in text
        assert "0.25%/天" in text
        assert query.answer.texts == ["成功领取 21 金币回报！"]

    def test_a_withdrawal_explains_the_principal_the_fee_and_the_reward(self, monkeypatch):
        outcome = stake_operations.WithdrawOutcome(
            stake_operations.WithdrawStatus.WITHDRAWN,
            fee=30,
            principal=970,
            reward=21,
            reward_due=21,
            intervals_passed=1,
        )

        query, _ = self.callback(monkeypatch, "withdraw", "withdraw_stake_principal", outcome)

        message = "您已取出质押本金 970 金币（手续费 30 金币），并获得回报 21 金币！"
        assert query.answer.texts == [message]
        assert query.edit_message_text.texts[0].startswith(message)

    def test_withdrawing_without_a_stake_is_an_alert(self, monkeypatch):
        query, _ = self.callback(
            monkeypatch,
            "withdraw",
            "withdraw_stake_principal",
            stake_operations.WithdrawOutcome(stake_operations.WithdrawStatus.NO_STAKE),
        )

        assert query.answer.texts == ["您没有质押任何金币。"]

    def test_a_failure_is_reported_with_a_reference(self, monkeypatch):
        query, _ = self.callback(
            monkeypatch, "collect", "collect_stake_reward", RuntimeError("db is down")
        )

        (text,) = query.answer.texts
        assert text.startswith("领取回报时发生错误，请稍后再试。") and "ERR-" in text

    @pytest.mark.parametrize(
        ("status", "balance_total", "fragment"),
        [
            (stake_operations.OpenStatus.NOT_REGISTERED, 0, "/me"),
            (stake_operations.OpenStatus.INSUFFICIENT, 40, "当前余额: 40 金币"),
            (stake_operations.OpenStatus.ALREADY_STAKED, 0, "已经有质押的金币"),
            (stake_operations.OpenStatus.STAKED, 0, "成功质押 100 金币"),
        ],
    )
    def test_stake_replies_follow_the_open_outcome(
        self, monkeypatch, status, balance_total, fragment
    ):
        calls = fake(
            monkeypatch,
            stake_operations,
            "open_stake",
            stake_operations.OpenStakeOutcome(status, balance_total),
        )
        fake(monkeypatch, stake_operations, "calculate_reward_rate", 0.25)
        update = command_update(chat_id=-100, message_id=11)

        run(stake_coin.stake_coins(update, command_context(), 100))

        assert calls == [((7, 100), {"op_key": "stake:-100:11"})]
        assert fragment in update.message.reply_text.texts[0]

    def test_a_replayed_stake_command_is_confirmed_like_a_success(self, monkeypatch):
        fake(
            monkeypatch,
            stake_operations,
            "open_stake",
            stake_operations.OpenStakeOutcome(stake_operations.OpenStatus.REPLAYED),
        )
        fake(monkeypatch, stake_operations, "calculate_reward_rate", 0.25)
        update = command_update()

        run(stake_coin.stake_coins(update, command_context(), 100))

        assert "成功质押 100 金币" in update.message.reply_text.texts[0]

    @pytest.mark.parametrize("amount", ["0", "-1", "abc"])
    def test_the_stake_amount_must_be_a_positive_integer(self, monkeypatch, amount):
        monkeypatch.setattr(
            stake_coin.process_user, "async_user_exists", Recorder(result=True)
        )
        calls = fake(monkeypatch, stake_operations, "open_stake", None)
        update = command_update()

        run(raw(stake_coin.stake_command)(update, command_context(amount)))

        assert "请输入有效的质押金额" in update.message.reply_text.texts[0]
        assert calls == []


class TestCharge:
    @pytest.mark.parametrize(
        ("result", "fragment"),
        [
            (charge_operations.RedeemResult(charge_operations.RedeemStatus.INVALID_FORMAT), "卡密格式无效"),
            (charge_operations.RedeemResult(charge_operations.RedeemStatus.BUSY), "正在被其他用户处理"),
            (charge_operations.RedeemResult(charge_operations.RedeemStatus.NOT_FOUND), "不存在或已被删除"),
            (charge_operations.RedeemResult(charge_operations.RedeemStatus.NOT_REGISTERED), "/me"),
            (
                charge_operations.RedeemResult(
                    charge_operations.RedeemStatus.ALREADY_USED,
                    used_at=datetime(2026, 10, 5, 8, 30, 1),
                    used_by_self=True,
                ),
                "已被您在 2026-10-05 08:30:01 使用过",
            ),
            (
                charge_operations.RedeemResult(
                    charge_operations.RedeemStatus.ALREADY_USED,
                    used_at=datetime(2026, 10, 5, 8, 30, 1),
                ),
                "已被其他用户在 2026-10-05 08:30:01 使用",
            ),
            (
                charge_operations.RedeemResult(charge_operations.RedeemStatus.ALREADY_USED),
                "未知时间",
            ),
            (
                charge_operations.RedeemResult(
                    charge_operations.RedeemStatus.FAILED, error_ref="ERR-ABCD1234"
                ),
                "ERR-ABCD1234",
            ),
        ],
    )
    def test_every_redeem_failure_has_a_user_message(self, result, fragment):
        assert fragment in charge_coin._redeem_failure_message(result)

    def test_admin_buttons_round_trip_and_reject_anything_else(self):
        for action in charge_operations.TopupAction:
            data = charge_coin.topup_admin_callback_data(action, 17)
            assert charge_coin.parse_topup_admin_callback(data) == (action, 17)

    def test_the_package_keyboard_encodes_price_and_coins(self):
        buttons = [
            button.callback_data
            for row in charge_coin._build_topup_keyboard().inline_keyboard
            for button in row
        ]

        assert buttons and all(data.startswith("topup_req_") for data in buttons)
        assert "topup_req_199_50" in buttons

    def test_a_non_admin_cannot_decide_a_request(self, monkeypatch, settings_override):
        settings_override(ADMIN_USER_ID=900)
        calls = fake(monkeypatch, charge_operations, "decide_topup_request", None)
        update, query = callback_update("topup_admin_approve_5", user_id=7)

        run(charge_coin.topup_admin_callback(update, command_context()))

        assert query.answer.texts == ["您没有权限处理该请求。"]
        assert calls == []

    def test_the_admin_id_is_read_when_the_handler_runs(self, monkeypatch, settings_override):
        settings_override(ADMIN_USER_ID=900)
        fake(monkeypatch, charge_operations, "get_topup_request", None)
        update, query = callback_update("topup_admin_approve_5", user_id=900)

        run(charge_coin.topup_admin_callback(update, command_context()))

        assert query.edit_message_text.texts == ["充值请求不存在（编号: #5）。"]

    def test_legacy_admin_buttons_never_credit(self, monkeypatch, settings_override):
        settings_override(ADMIN_USER_ID=900)
        calls = fake(monkeypatch, charge_operations, "decide_topup_request", None)
        update, query = callback_update("topup_admin_approve_1_50_199", user_id=900)

        run(charge_coin.topup_admin_callback(update, command_context()))

        assert "已失效，不会发放金币" in query.edit_message_text.texts[0]
        assert calls == []


class TestWebPassword:
    def run_command(self, monkeypatch, *args, record=None, saved=None):
        fake(monkeypatch, user_records, "async_check_user_exists", True)
        fake(monkeypatch, web_password_operations, "get_user_web_password", record)
        calls = fake(monkeypatch, web_password_operations, "process_set_web_password", saved)
        update = command_update()
        run(raw(web_password.webpassword_command)(update, command_context(*args)))
        return update, calls

    def test_without_arguments_it_shows_the_status(self, monkeypatch):
        record = WebPasswordRecord(
            "$argon2id$x", datetime(2026, 1, 2, 3, 4, 5), datetime(2026, 2, 3, 4, 5, 6)
        )

        update, calls = self.run_command(monkeypatch, record=record)

        text = update.message.reply_text.texts[0]
        assert "状态: <b>已设置</b>" in text
        assert "2026-01-02 03:04:05" in text and "2026-02-03 04:05:06" in text
        assert "$argon2id" not in text
        assert calls == []

    def test_without_a_password_set_it_says_so(self, monkeypatch):
        update, _ = self.run_command(monkeypatch)

        assert "状态: <b>未设置</b>" in update.message.reply_text.texts[0]

    def test_a_valid_password_is_forwarded_and_never_echoed(self, monkeypatch):
        saved = web_password_operations.SetPasswordResult(
            web_password_operations.SetPasswordStatus.SAVED, is_update=True
        )

        update, calls = self.run_command(monkeypatch, "abc12345", saved=saved)

        assert calls == [((7, "abc12345"), {})]
        text = update.message.reply_text.texts[0]
        assert "Web密码更新成功" in text
        assert "abc12345" not in text

    def test_an_invalid_password_shows_the_reason_and_the_rules(self, monkeypatch):
        saved = web_password_operations.SetPasswordResult(
            web_password_operations.SetPasswordStatus.INVALID, "密码长度必须在6-20位之间"
        )

        update, _ = self.run_command(monkeypatch, "abc", saved=saved)

        text = update.message.reply_text.texts[0]
        assert "密码长度必须在6-20位之间" in text and "密码要求" in text
        assert "abc\n" not in text

    def test_a_storage_failure_is_a_generic_message(self, monkeypatch):
        saved = web_password_operations.SetPasswordResult(
            web_password_operations.SetPasswordStatus.FAILED
        )

        update, _ = self.run_command(monkeypatch, "abc12345", saved=saved)

        assert "设置Web密码时发生错误，请稍后再试" in update.message.reply_text.texts[0]


class TestInvitations:
    def bind(self, monkeypatch, *args, outcome=None, referrer=None, exists=True, user_id=7):
        outcome = outcome or invitation_operations.InvitationOutcome(True, True)
        calls = fake(monkeypatch, invitation_operations, "add_invitation_record", outcome)
        fake(monkeypatch, invitation_operations, "get_referrer", referrer)
        fake(monkeypatch, invitation_operations, "user_exists", exists)
        update = command_update(user_id=user_id)
        run(raw(ref.ref_command)(update, command_context(*args)))
        return update, calls

    def test_binding_a_new_user_reports_both_rewards(self, monkeypatch, settings_override):
        settings_override(NEW_USER_BONUS_COINS=5)

        update, calls = self.bind(monkeypatch, "9")

        assert calls == [((7, 9, "Kc Fog"), {})]
        text = update.message.reply_text.texts[0]
        assert "邀请绑定成功" in text and "*20* 邀请奖励" in text and "*5* 新人奖励（共 *25* 金币）" in text

    def test_the_new_user_bonus_is_read_when_the_message_is_built(self, monkeypatch, settings_override):
        settings_override(NEW_USER_BONUS_COINS=8)

        update, _ = self.bind(monkeypatch, "9")

        assert "*8* 新人奖励（共 *28* 金币）" in update.message.reply_text.texts[0]

    def test_binding_an_existing_user_reports_only_the_invitation_reward(self, monkeypatch):
        outcome = invitation_operations.InvitationOutcome(True, False)

        update, _ = self.bind(monkeypatch, "9", outcome=outcome)

        text = update.message.reply_text.texts[0]
        assert "*20* 邀请奖励" in text and "新人奖励" not in text

    def test_inviting_yourself_is_refused_before_anything_runs(self, monkeypatch):
        update, calls = self.bind(monkeypatch, "7")

        assert update.message.reply_text.texts == ["您不能邀请自己哦！"]
        assert calls == []

    def test_an_invitation_code_must_be_a_number(self, monkeypatch):
        update, calls = self.bind(monkeypatch, "abc")

        assert update.message.reply_text.texts == ["邀请码必须是数字！"]
        assert calls == []

    def test_an_already_invited_user_sees_the_current_referrer(self, monkeypatch):
        update, calls = self.bind(monkeypatch, "9", referrer=Referrer(3, "carol"))

        assert "已经被 *carol* (`3`) 邀请过了" in update.message.reply_text.texts[0]
        assert calls == []

    def test_a_missing_referrer_is_reported(self, monkeypatch):
        outcome = invitation_operations.InvitationOutcome(False, False)

        update, _ = self.bind(monkeypatch, "9", outcome=outcome, exists=False)

        assert "邀请人不存在" in update.message.reply_text.texts[0]

    def test_other_failures_are_reported_as_system_errors(self, monkeypatch):
        outcome = invitation_operations.InvitationOutcome(False, False)

        update, _ = self.bind(monkeypatch, "9", outcome=outcome, exists=True)

        assert "可能是系统错误" in update.message.reply_text.texts[0]

    def test_without_arguments_it_shows_the_invitation_summary(self, monkeypatch):
        fake(
            monkeypatch,
            invitation_operations,
            "get_invitation_summary",
            invitation_operations.InvitationSummary(
                2,
                [
                    InvitedUser(11, "dave", datetime(2026, 10, 5, 8, 0, 0)),
                    InvitedUser(12, "erin", datetime(2026, 10, 4, 8, 0, 0)),
                ],
            ),
        )
        fake(monkeypatch, invitation_operations, "get_referrer", Referrer(3, "carol"))
        update = command_update()

        run(raw(ref.ref_command)(update, command_context()))

        text = update.message.reply_text.texts[0]
        assert "已邀请人数：*2*" in text and "已获得奖励：*40* 金币" in text
        assert "您的邀请人：*carol* (`3`)" in text
        assert "1. dave (`11`) - 2026-10-05 08:00:00" in text
        assert "https://t.me/fogmoe_bot?start=7" in text

    def test_a_start_link_from_yourself_or_garbage_is_ignored(self, monkeypatch):
        calls = fake(
            monkeypatch,
            invitation_operations,
            "add_invitation_record",
            invitation_operations.InvitationOutcome(True, True),
        )
        for args in (("7",), ("abc",), ()):
            update = command_update()
            assert run(ref.process_start_with_args(update, command_context(*args))) is False

        assert calls == []

    def test_a_start_link_rewards_and_names_the_referrer(self, monkeypatch):
        fake(
            monkeypatch,
            invitation_operations,
            "add_invitation_record",
            invitation_operations.InvitationOutcome(True, False),
        )
        fake(monkeypatch, invitation_operations, "get_user_name", "carol")
        update = command_update()

        assert run(ref.process_start_with_args(update, command_context("9"))) is True

        text = update.message.reply_text.texts[0]
        assert "通过邀请链接加入" in text and "您的邀请人是：@boss" in text

    def test_a_start_link_that_did_not_record_is_not_acknowledged(self, monkeypatch):
        fake(
            monkeypatch,
            invitation_operations,
            "add_invitation_record",
            invitation_operations.InvitationOutcome(False, False),
        )
        update = command_update()

        assert run(ref.process_start_with_args(update, command_context("9"))) is False
        assert update.message.reply_text.calls == []


class TestBribe:
    def bribe(self, monkeypatch, *args, outcome=None, affection=10):
        fake(monkeypatch, bribe.process_user, "async_get_user_affection", affection)
        calls = fake(monkeypatch, bribe_operations, "pay_bribe", outcome)
        update = command_update(chat_id=-100, message_id=11)
        run(raw(bribe.bribe_command)(update, command_context(*args)))
        return update, calls

    @pytest.mark.parametrize(
        ("args", "fragment"),
        [
            ((), "用法：/bribe"),
            (("abc",), "请输入有效的金币数量"),
            (("-100",), "必须为正整数"),
            (("50",), "至少需要 100 枚金币"),
            (("150",), "100 的整数倍"),
        ],
    )
    def test_invalid_amounts_are_refused_before_any_charge(self, monkeypatch, args, fragment):
        update, calls = self.bribe(monkeypatch, *args)

        assert fragment in update.message.reply_text.texts[0]
        assert calls == []

    def test_a_full_affection_meter_refuses_to_take_coins(self, monkeypatch):
        update, calls = self.bribe(monkeypatch, "100", affection=100)

        assert "没有上限可涨了" in update.message.reply_text.texts[0]
        assert calls == []

    def test_a_paid_bribe_reports_the_gain(self, monkeypatch):
        outcome = bribe_operations.BribeOutcome(
            bribe_operations.BribeStatus.PAID, total_gain=7, affection_after=17
        )

        update, calls = self.bribe(monkeypatch, "200", outcome=outcome)

        assert calls == [((7, 200, 10, "bribe:-100:11"), {})]
        assert "心情改善了 7 点" in update.message.reply_text.texts[0]
        assert "10 → 17" in update.message.reply_text.texts[0]

    def test_an_insufficient_balance_is_explained(self, monkeypatch):
        outcome = bribe_operations.BribeOutcome(
            bribe_operations.BribeStatus.INSUFFICIENT, balance_total=120
        )

        update, _ = self.bribe(monkeypatch, "200", outcome=outcome)

        assert "当前拥有 120 枚，无法支付 200 枚" in update.message.reply_text.texts[0]

    def test_a_redelivered_command_is_silent(self, monkeypatch):
        outcome = bribe_operations.BribeOutcome(bribe_operations.BribeStatus.REPLAYED)

        update, _ = self.bribe(monkeypatch, "200", outcome=outcome)

        assert update.message.reply_text.calls == []

    def test_a_failure_is_a_generic_reply(self, monkeypatch):
        update, _ = self.bribe(monkeypatch, "200", outcome=RuntimeError("db is down"))

        assert update.message.reply_text.texts == ["贿赂过程中出现问题，请稍后再试。"]
