"""一轮对话的业务操作：纯函数与各阶段，全部用替身，不起 bot、不连数据库、不碰模型。"""

import asyncio
import dataclasses
from types import SimpleNamespace

import pytest

from core import telegram_history
from features.ai import router
from features.conversation import billing, turn
from features.conversation.turn_services import TurnServices
from features.conversation.turn_types import (
    ChatRef,
    ConversationSettings,
    IncomingMessage,
    ModelResponse,
    SenderRef,
    Stage,
    StageTimer,
    TurnRequest,
    TurnStatus,
    UserStateRecord,
)

# ---------------------------------------------------------------------------
# 价格与纯函数
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("length", "cost"),
    [
        (1, 1),
        (100, 1),
        (101, 2),
        (500, 2),
        (501, 3),
        (1000, 3),
        (1001, 4),
        (2000, 4),
        (2001, 5),
        (4096, 5),
    ],
)
def test_text_message_cost_tiers(length, cost):
    assert billing.text_message_cost(length) == cost


def test_impression_is_flattened_truncated_and_has_a_placeholder():
    assert turn.format_impression(None) == "Not recorded"
    assert turn.format_impression("  \n ") == "Not recorded"
    assert turn.format_impression("喜欢\r\n猫") == "喜欢  猫"

    long_text = "x" * 600
    shortened = turn.format_impression(long_text)
    assert len(shortened) == 500 and shortened.endswith("...")


def test_personal_info_is_trimmed_and_capped_without_an_ellipsis():
    assert turn.format_personal_info(None) == ""
    assert turn.format_personal_info("  hi  ") == "hi"
    assert turn.format_personal_info("y" * 700) == "y" * 500


@pytest.mark.parametrize(
    ("levels", "expected"),
    [
        ([], None),
        ([None, ""], None),
        (["near_limit"], "near_limit"),
        (["near_limit", "overflow"], "overflow"),
        (["overflow", "near_limit"], "overflow"),
        (["near_limit", "other"], "near_limit"),
    ],
)
def test_history_warnings_merge_with_overflow_taking_priority(levels, expected):
    merged = None
    for level in levels:
        merged = turn.merge_history_warning(merged, level)

    assert merged == expected


def test_history_warning_text_only_exists_for_known_levels():
    assert turn.history_warning_text("near_limit")
    assert turn.history_warning_text("overflow")
    assert turn.history_warning_text("other") is None
    assert turn.history_warning_text(None) is None


def test_stage_timer_accumulates_per_stage_and_reports_queue_time():
    ticks = iter([10.0, 10.0, 12.5, 12.5, 13.0, 14.0, 15.0, 20.0])
    timer = StageTimer(queue_seconds=0.25, clock=lambda: next(ticks))

    with timer.stage(Stage.CHARGE):
        pass
    with timer.stage(Stage.MODEL):
        pass
    with timer.stage(Stage.CHARGE):
        pass
    timings = timer.snapshot()

    assert timings.queue_seconds == 0.25
    assert timings.seconds(Stage.CHARGE) == pytest.approx(3.5)
    assert timings.seconds(Stage.MODEL) == pytest.approx(0.5)
    assert timings.seconds(Stage.DELIVERY) == 0.0
    assert timings.run_seconds == pytest.approx(10.0)
    assert "queue=0.250s" in timings.summary() and "model=0.500s" in timings.summary()


# ---------------------------------------------------------------------------
# 替身
# ---------------------------------------------------------------------------

CHARGED = billing.TurnCharge(
    status=billing.TurnChargeStatus.CHARGED,
    total_cost=1,
    newly_charged=1,
    permission=2,
    info="喜欢茶",
    balance_free=6,
    balance_paid=4,
)


class World:
    """记录一轮对话里对外部世界的全部调用，按发生顺序保存在 `events`。"""

    def __init__(self):
        self.events: list[tuple] = []
        self.charge_result = CHARGED
        self.history: list[dict] = [{"role": "user", "content": "<history/>"}]
        self.insert_results: list[tuple] = []
        self.model_response = ModelResponse(text="你好呀", tool_logs=[])
        self.visible_sent: list = []
        self.model_requests: list = []
        self.suppressed_during_model: list[bool] = []
        self.sent_message_ids = iter(range(900, 1000))

    def names(self) -> list[str]:
        return [event[0] for event in self.events]

    def calls(self, name: str) -> list[tuple]:
        return [event for event in self.events if event[0] == name]


def make_services(world: World, **overrides) -> TurnServices:
    async def charge(user_id, messages):
        world.events.append(("charge", user_id, [message.cost for message in messages]))
        return world.charge_result

    async def flush_events(conversation_id):
        world.events.append(("flush", conversation_id))

    async def load_user_state(user_id):
        world.events.append(("load_user_state", user_id))
        return UserStateRecord(impression="很可爱", diary_exists=True)

    async def insert_records(conversation_id, entries, **options):
        world.events.append(("insert_records", conversation_id, list(entries), options))
        return world.insert_results.pop(0) if world.insert_results else (False, None, [])

    async def insert_record(conversation_id, role, content, **options):
        world.events.append(("insert_record", conversation_id, role, content))
        return world.insert_results.pop(0) if world.insert_results else (False, None, [])

    async def get_history(conversation_id):
        world.events.append(("get_history", conversation_id))
        return list(world.history)

    def schedule_summary(conversation_id):
        world.events.append(("schedule_summary", conversation_id))

    async def handle_history_overflow(conversation_id):
        world.events.append(("handle_overflow", conversation_id))

    async def arm_idle_followup(user_id):
        world.events.append(("arm_idle_followup", user_id))

    async def archive_completed_clear(**kwargs):
        world.events.append(("archive_clear", kwargs))

    async def analyze_image(base64_str):
        world.events.append(("analyze_image", base64_str))
        return "一只猫"

    async def run_model(request):
        world.events.append(("run_model", request.user_id))
        world.model_requests.append(request)
        world.suppressed_during_model.append(telegram_history._CAPTURE_SUPPRESSED.get())
        return world.model_response

    def make_visible_handler(**kwargs):
        world.events.append(("make_visible_handler", kwargs["reply_to_message_id"]))
        return SimpleNamespace(sent_messages=list(world.visible_sent))

    async def reply_text(message, text):
        world.events.append(("reply_text", message.message_id, text))

    async def send_typing(bot, chat_id):
        world.events.append(("typing", chat_id))

    async def send_warning(bot, chat_id, text):
        world.events.append(("warning", chat_id, text))

    async def send_archive(bot, user_id, records):
        world.events.append(("archive", user_id, records))

    async def normalize_stickers(text):
        world.events.append(("normalize_stickers", text))
        return text

    async def send_reply(**kwargs):
        world.events.append(("send_reply", kwargs))
        message = SimpleNamespace(message_id=next(world.sent_message_ids))
        return [message]

    async def send_generated_media(**kwargs):
        world.events.append(("generated_media", kwargs["tool_logs"]))
        return []

    async def log_group_message(message, chat_id):
        world.events.append(("group_log", message.message_id, chat_id))

    services = TurnServices(
        charge=charge,
        flush_events=flush_events,
        load_user_state=load_user_state,
        insert_records=insert_records,
        insert_record=insert_record,
        get_history=get_history,
        schedule_summary=schedule_summary,
        handle_history_overflow=handle_history_overflow,
        arm_idle_followup=arm_idle_followup,
        archive_completed_clear=archive_completed_clear,
        analyze_image=analyze_image,
        run_model=run_model,
        make_visible_handler=make_visible_handler,
        reply_text=reply_text,
        send_typing=send_typing,
        send_warning=send_warning,
        send_archive=send_archive,
        normalize_stickers=normalize_stickers,
        send_reply=send_reply,
        send_generated_media=send_generated_media,
        log_group_message=log_group_message,
    )
    return dataclasses.replace(services, **overrides)


def text_message(message_id=1, text="你好", **extra):
    async def reply_text(text, **kwargs):
        return None

    values = dict(
        message_id=message_id,
        text=text,
        caption=None,
        photo=None,
        sticker=None,
        date=None,
        edit_date=None,
        reply_to_message=None,
        reply_text=reply_text,
    )
    values.update(extra)
    return SimpleNamespace(**values)


def photo_message(message_id=2, *, file_size=3, data=b"img", caption=None):
    async def get_file():
        async def download_as_bytearray():
            return bytearray(data)

        return SimpleNamespace(file_size=file_size, download_as_bytearray=download_as_bytearray)

    return text_message(
        message_id,
        text=None,
        caption=caption,
        photo=[SimpleNamespace(get_file=get_file)],
    )


async def _bot_send_message(*args, **kwargs):
    return None


def make_request(*messages, chat_type="private", edited=(), queue_seconds=0.0) -> TurnRequest:
    return TurnRequest(
        chat=ChatRef(chat_id=100, chat_type=chat_type, title="群" if chat_type != "private" else None),
        sender=SenderRef(user_id=7, username="kc", first_name="K", language_code="zh"),
        messages=tuple(
            IncomingMessage(message=message, edited=message.message_id in edited, update_id=None)
            for message in messages
        ),
        bot=SimpleNamespace(send_message=_bot_send_message),
        queue_seconds=queue_seconds,
    )


def run_turn(request, services, settings=None):
    return asyncio.run(
        turn.ConversationTurn(request, services, settings or ConversationSettings()).run()
    )


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


class TestPlan:
    def test_a_message_that_is_too_long_ends_the_turn_before_any_charge(self):
        world = World()
        long_message = text_message(2, "长" * 4097)

        result = run_turn(make_request(text_message(1), long_message), make_services(world))

        assert result.status is TurnStatus.MESSAGE_TOO_LONG
        assert world.names() == ["reply_text"]
        assert world.events[0][1:] == (2, turn.MESSAGE_TOO_LONG_TEXT)

    def test_messages_without_text_or_media_are_ignored_and_cost_nothing(self):
        world = World()

        result = run_turn(make_request(text_message(1, None)), make_services(world))

        assert result.status is TurnStatus.NOTHING_TO_PROCESS
        assert world.events == []

    def test_each_message_is_priced_by_its_own_length_or_as_media(self):
        world = World()
        request = make_request(
            text_message(1, "短"),
            text_message(2, "长" * 150),
            photo_message(3),
        )

        run_turn(request, make_services(world))

        assert world.calls("charge")[0][2] == [1, 2, billing.MEDIA_COST]


# ---------------------------------------------------------------------------
# charge
# ---------------------------------------------------------------------------


class TestCharge:
    def test_pending_history_is_flushed_before_the_charge(self):
        world = World()

        run_turn(make_request(text_message()), make_services(world))

        assert world.names()[:2] == ["flush", "charge"]

    def test_insufficient_balance_replies_with_the_total_cost_and_stops(self):
        world = World()
        world.charge_result = billing.TurnCharge(
            billing.TurnChargeStatus.INSUFFICIENT, total_cost=3, balance_free=1
        )
        request = make_request(text_message(1, "短"), text_message(2, "长" * 150))

        result = run_turn(request, make_services(world))

        assert result.status is TurnStatus.INSUFFICIENT_BALANCE
        assert result.charge is world.charge_result
        reply = world.calls("reply_text")[0]
        assert reply[1] == 2  # 回复挂在最后一条消息上
        assert "需要3个硬币" in reply[2] and "need 3" in reply[2]
        assert "run_model" not in world.names() and "insert_records" not in world.names()

    def test_an_unregistered_user_is_asked_to_register_and_nothing_else_happens(self):
        world = World()
        world.charge_result = billing.TurnCharge(billing.TurnChargeStatus.UNREGISTERED, 1)

        result = run_turn(make_request(text_message()), make_services(world))

        assert result.status is TurnStatus.UNREGISTERED
        assert world.calls("reply_text")[0][2] == turn.UNREGISTERED_TEXT
        assert "load_user_state" not in world.names()


# ---------------------------------------------------------------------------
# 完整的一轮
# ---------------------------------------------------------------------------


class TestCompletedTurn:
    def test_a_private_turn_runs_the_stages_in_order(self):
        world = World()

        result = run_turn(make_request(text_message(5, "你好")), make_services(world))

        assert result.status is TurnStatus.COMPLETED
        assert result.sent_message_count == 1
        assert world.names() == [
            "flush",
            "charge",
            "load_user_state",
            "insert_records",  # 用户消息
            "arm_idle_followup",
            "get_history",
            "typing",
            "make_visible_handler",
            "run_model",
            "normalize_stickers",
            "insert_record",  # AI 回复
            "typing",
            "send_reply",
            "generated_media",
            "flush",
            "insert_records",  # 零余额边界
        ]
        user_write = world.calls("insert_records")[0]
        assert user_write[2][0][0] == "user" and "你好" in user_write[2][0][1]
        assert user_write[3]["allow_zero_balance"] is True
        assert "coins" in user_write[3]["system_prompt_extra"]
        assert world.calls("insert_record")[0][2:] == ("assistant", "你好呀")
        assert world.calls("insert_records")[-1][3] == {"suspend_if_zero": True}
        assert [stage.value for stage in result.timings.stages] == [
            "plan",
            "charge",
            "context",
            "prepare",
            "history_in",
            "model",
            "history_out",
            "delivery",
            "finalize",
        ]

    def test_the_user_state_prompt_reflects_the_charge_and_profile(self):
        world = World()

        run_turn(make_request(text_message()), make_services(world))

        prompt = world.model_requests[0].tool_context["user_state_prompt"]
        assert 'coins="10"' in prompt
        assert 'user_plan="paid"' in prompt
        assert 'permission="2"' in prompt
        assert 'diary_exists="true"' in prompt
        assert "很可爱" in prompt and "喜欢茶" in prompt

    def test_the_model_request_carries_history_context_and_runs_without_history_capture(self):
        world = World()
        request = make_request(text_message(9, "你好"), chat_type="supergroup")

        run_turn(request, make_services(world))

        (model_request,) = world.model_requests
        assert model_request.messages == world.history
        assert model_request.text_fallback_messages == world.history
        assert model_request.user_id == 7
        assert model_request.tool_context["is_group"] is True
        assert model_request.tool_context["group_id"] == 100
        assert model_request.tool_context["message_id"] == 9
        assert model_request.tool_context["username"] == "kc"
        assert model_request.visible_content_handler is not None
        # 模型执行期间 bot 自己发出的消息不由历史观察器记录，本轮自己按顺序写。
        assert world.suppressed_during_model == [True]
        assert telegram_history._CAPTURE_SUPPRESSED.get() is False

    def test_the_fogmoebot_command_is_answered_but_not_written_twice(self):
        world = World()

        run_turn(make_request(text_message(1, "/fogmoebot 你好")), make_services(world))

        writes = world.calls("insert_records")
        assert [write[3] for write in writes] == [{"suspend_if_zero": True}]
        assert "run_model" in world.names()

    def test_an_empty_model_reply_sends_and_records_nothing(self):
        world = World()
        world.model_response = ModelResponse(text="  ", tool_logs=[])

        result = run_turn(make_request(text_message()), make_services(world))

        assert result.status is TurnStatus.COMPLETED
        assert result.sent_message_count == 0
        assert "send_reply" not in world.names() and "insert_record" not in world.names()

    def test_an_error_notice_is_delivered_but_never_written_as_an_assistant_record(self):
        world = World()
        world.model_response = ModelResponse(text=router.AI_SERVICE_ERROR_MESSAGE, tool_logs=[])

        result = run_turn(make_request(text_message()), make_services(world))

        assert result.runtime_error == "all_ai_services_failed"
        assert "insert_record" not in world.names()
        assert world.calls("send_reply")[0][1]["text"] == router.AI_SERVICE_ERROR_MESSAGE

    def test_tool_results_are_recorded_before_the_assistant_reply(self):
        world = World()
        world.model_response = ModelResponse(
            text="查好了",
            tool_logs=[
                {"type": "telegram_event", "content": "<event/>"},
                {"type": "assistant_visible", "content": "稍等"},
            ],
        )

        run_turn(make_request(text_message()), make_services(world))

        names = world.names()
        tool_write = [e for e in world.calls("insert_records") if e[3] == {"allow_zero_balance": True}]
        assert tool_write[-1][2] == [("user", "<event/>"), ("assistant", "稍等")]
        assert names.index("insert_record") > names.index("run_model")
        assert world.calls("generated_media")[0][1] is world.model_response.tool_logs

    def test_a_completed_clear_archives_the_turn_instead_of_recording_it(self):
        world = World()
        world.model_response = ModelResponse(
            text="已清空",
            tool_logs=[
                {
                    "type": "tool_result",
                    "tool_name": "execute_telegram_command",
                    "arguments": {"command": "/clear"},
                    "result": {"success": True},
                    "tool_call_id": "call-1",
                }
            ],
        )

        run_turn(make_request(text_message()), make_services(world))

        names = world.names()
        assert "insert_record" not in names
        (archive,) = world.calls("archive_clear")
        assert archive[1]["assistant_message"] == "已清空"
        assert archive[1]["conversation_id"] == 7
        # 归档在本轮所有展示之后、最后的零余额边界之前
        assert names.index("archive_clear") > names.index("send_reply")
        assert names.index("archive_clear") < len(names) - 2

    def test_visible_content_already_sent_makes_the_final_reply_a_plain_send(self):
        world = World()
        world.visible_sent = [SimpleNamespace(message_id=500)]
        request = make_request(text_message(5))

        run_turn(request, make_services(world))

        kwargs = world.calls("send_reply")[0][1]
        assert kwargs["reply_to_message_id"] is None
        assert kwargs["first_text_send"] is not request.reply_target.reply_text

    def test_without_earlier_visible_content_the_reply_threads_under_the_last_message(self):
        world = World()
        request = make_request(text_message(5), text_message(6))

        run_turn(request, make_services(world))

        kwargs = world.calls("send_reply")[0][1]
        assert kwargs["reply_to_message_id"] == 6
        assert kwargs["first_text_send"] == request.reply_target.reply_text

    def test_group_replies_are_logged_to_the_group_history_except_error_notices(self):
        world = World()

        run_turn(make_request(text_message(), chat_type="group"), make_services(world))
        logged = world.calls("group_log")
        assert [event[1:] for event in logged] == [(900, 100)]

        world = World()
        world.model_response = ModelResponse(text=router.AI_SERVICE_ERROR_MESSAGE, tool_logs=[])
        run_turn(make_request(text_message(), chat_type="group"), make_services(world))
        assert world.calls("group_log") == []

    def test_private_replies_are_not_logged_to_the_group_history(self):
        world = World()

        run_turn(make_request(text_message()), make_services(world))

        assert world.calls("group_log") == []

    def test_group_turns_do_not_arm_the_idle_followup(self):
        world = World()

        run_turn(make_request(text_message(), chat_type="supergroup"), make_services(world))

        assert "arm_idle_followup" not in world.names()

    def test_queue_time_is_carried_into_the_timings(self):
        world = World()

        result = run_turn(make_request(text_message(), queue_seconds=1.5), make_services(world))

        assert result.timings.queue_seconds == 1.5


# ---------------------------------------------------------------------------
# 媒体
# ---------------------------------------------------------------------------


class TestMedia:
    def test_a_photo_is_described_for_history_and_sent_to_the_model_as_an_image(self):
        world = World()
        captured = {}

        async def insert_records(conversation_id, entries, **options):
            captured.setdefault("entries", list(entries))
            return (False, None, [])

        # 历史里读回的就是刚写入的描述文本，运行时消息据此把它换成带原图的版本。
        async def get_history(conversation_id):
            return [{"role": "user", "content": captured["entries"][0][1]}]

        services = make_services(world, insert_records=insert_records, get_history=get_history)

        run_turn(make_request(photo_message(3, caption="看")), services)

        description = captured["entries"][0][1]
        assert "一只猫" in description and "看" in description
        (model_request,) = world.model_requests
        (runtime_message,) = model_request.messages
        assert runtime_message["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
        assert "一只猫" not in runtime_message["content"][0]["text"]
        assert model_request.text_fallback_messages == [
            {"role": "user", "content": description}
        ]

    def test_an_oversized_file_is_refused_after_the_charge_without_a_refund(self):
        world = World()
        settings = ConversationSettings(max_media_download_bytes=10)

        result = run_turn(
            make_request(photo_message(3, file_size=11)),
            make_services(world),
            settings,
        )

        assert result.status is TurnStatus.MEDIA_TOO_LARGE
        assert world.calls("reply_text")[0][2] == turn.MEDIA_TOO_LARGE_TEXT
        assert "charge" in world.names()
        assert "analyze_image" not in world.names() and "run_model" not in world.names()

    def test_a_download_that_turns_out_larger_than_the_limit_is_refused_too(self):
        world = World()
        settings = ConversationSettings(max_media_download_bytes=2)

        result = run_turn(
            make_request(photo_message(3, file_size=None, data=b"abcd")),
            make_services(world),
            settings,
        )

        assert result.status is TurnStatus.MEDIA_TOO_LARGE

    def test_a_failing_image_analysis_replies_with_an_apology_and_stops(self):
        world = World()

        async def broken(base64_str):
            raise RuntimeError("vision down")

        result = run_turn(
            make_request(photo_message(3)),
            make_services(world, analyze_image=broken),
        )

        assert result.status is TurnStatus.MEDIA_FAILED
        assert world.calls("reply_text")[0][2] == turn.MEDIA_FAILED_TEXT
        assert "insert_records" not in world.names()


# ---------------------------------------------------------------------------
# 历史写入的收尾：容量提示、溢出、归档、摘要
# ---------------------------------------------------------------------------


class TestHistoryAftermath:
    def test_a_capacity_warning_is_announced_once_before_the_final_reply(self):
        world = World()
        world.insert_results = [(False, "near_limit", []), (False, "near_limit", [])]

        run_turn(make_request(text_message()), make_services(world))

        names = world.names()
        warnings = world.calls("warning")
        assert len(warnings) == 1 and warnings[0][2] == turn.NEAR_LIMIT_WARNING_TEXT
        assert names.index("warning") < names.index("send_reply")

    def test_overflow_summarizes_immediately_and_does_not_queue_a_second_summary(self):
        world = World()
        world.insert_results = [(True, "overflow", [])]

        run_turn(make_request(text_message()), make_services(world))

        assert world.calls("handle_overflow") == [("handle_overflow", 7)]
        assert world.calls("schedule_summary") == []
        assert world.calls("warning")[0][2] == turn.OVERFLOW_WARNING_TEXT

    def test_a_new_snapshot_without_overflow_queues_a_summary(self):
        world = World()
        world.insert_results = [(True, None, [])]

        run_turn(make_request(text_message()), make_services(world))

        assert world.calls("schedule_summary") == [("schedule_summary", 7)]

    def test_archived_records_are_sent_to_the_user(self):
        world = World()
        world.insert_results = [(False, None, [{"role": "user"}])]

        run_turn(make_request(text_message()), make_services(world))

        assert world.calls("archive") == [("archive", 7, [{"role": "user"}])]

    def test_the_zero_balance_boundary_announces_its_warning_immediately(self):
        world = World()
        world.insert_results = [(False, None, []), (False, None, []), (False, "overflow", [])]

        run_turn(make_request(text_message()), make_services(world))

        names = world.names()
        assert world.calls("warning")[0][2] == turn.OVERFLOW_WARNING_TEXT
        assert names.index("warning") > names.index("send_reply")


# ---------------------------------------------------------------------------
# 模型执行是单一的替换点
# ---------------------------------------------------------------------------


def test_the_model_stage_is_one_replaceable_call_returning_typed_data():
    world = World()
    calls = []

    async def replacement(request):
        calls.append(request)
        return ModelResponse(text="换了一个模型", tool_logs=[])

    run_turn(make_request(text_message()), make_services(world, run_model=replacement))

    assert len(calls) == 1
    assert world.calls("run_model") == []
    assert world.calls("insert_record")[0][3] == "换了一个模型"
