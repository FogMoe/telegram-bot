"""三个 AI 入口（用户消息、定时任务、空闲跟进）共用收尾流水线与 EntryPolicy 的单测。"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from types import SimpleNamespace

import pytest

from fogmoe_telegram_bot.core import telegram_history
from fogmoe_telegram_bot.features.ai import idle_followup, scheduler
from fogmoe_telegram_bot.features.ai.router import (
    AI_SERVICE_ERROR_MESSAGE,
    PARTIAL_AI_RESPONSE_ERROR_MESSAGE,
)
from fogmoe_telegram_bot.features.ai.turn_pipeline import (
    ENTRY_POLICIES,
    IDLE_FOLLOWUP_ENTRY_POLICY,
    SCHEDULE_ENTRY_POLICY,
    USER_ENTRY_POLICY,
    NoSideEffectsRetryError,
    PostInputStage,
    RuntimeErrorPolicy,
    execute_post_input_turn,
    prepare_model_turn,
)
from fogmoe_telegram_bot.features.ai.types import LoopResult, LoopStatus

logger = logging.getLogger(__name__)


async def _identity_stickers(text: str) -> str:
    return text


def test_entry_policies_declare_expected_boundaries() -> None:
    assert set(ENTRY_POLICIES) == {"user", "schedule", "idle_followup"}

    assert USER_ENTRY_POLICY.suspend_if_zero is True
    assert USER_ENTRY_POLICY.supports_completed_clear is True
    assert USER_ENTRY_POLICY.runtime_error_policy is RuntimeErrorPolicy.DELIVER_NOTICE
    assert USER_ENTRY_POLICY.send_typing_before_model is True
    assert USER_ENTRY_POLICY.stream_visible_content is True

    assert SCHEDULE_ENTRY_POLICY.suspend_if_zero is False
    assert SCHEDULE_ENTRY_POLICY.supports_completed_clear is False
    assert SCHEDULE_ENTRY_POLICY.runtime_error_policy is RuntimeErrorPolicy.DELIVER_NOTICE

    assert IDLE_FOLLOWUP_ENTRY_POLICY.suspend_if_zero is False
    assert IDLE_FOLLOWUP_ENTRY_POLICY.supports_completed_clear is False
    assert IDLE_FOLLOWUP_ENTRY_POLICY.runtime_error_policy is RuntimeErrorPolicy.RETRY_OR_SUPPRESS
    assert IDLE_FOLLOWUP_ENTRY_POLICY.send_typing_before_model is False
    assert IDLE_FOLLOWUP_ENTRY_POLICY.stream_visible_content is False


def test_prepare_model_turn_applies_entry_runtime_error_policies() -> None:
    suppressed_flags: list[bool] = []

    async def failing_without_tools(_visible):
        suppressed_flags.append(telegram_history._CAPTURE_SUPPRESSED.get())
        return LoopResult(
            text=AI_SERVICE_ERROR_MESSAGE,
            tool_logs=[],
            status=LoopStatus.ALL_PROVIDERS_FAILED,
        )

    user_turn = asyncio.run(
        prepare_model_turn(
            policy=USER_ENTRY_POLICY,
            invoke_model=failing_without_tools,
            normalize_stickers=_identity_stickers,
            logger=logger,
            user_id=10,
        )
    )
    assert suppressed_flags == [True]
    assert user_turn.text == AI_SERVICE_ERROR_MESSAGE
    assert user_turn.runtime_error == "all_ai_services_failed"
    assert user_turn.reply_entries == []

    schedule_turn = asyncio.run(
        prepare_model_turn(
            policy=SCHEDULE_ENTRY_POLICY,
            invoke_model=failing_without_tools,
            normalize_stickers=_identity_stickers,
            logger=logger,
            user_id=10,
        )
    )
    assert schedule_turn.text == AI_SERVICE_ERROR_MESSAGE
    assert schedule_turn.runtime_error == "all_ai_services_failed"

    with pytest.raises(NoSideEffectsRetryError):
        asyncio.run(
            prepare_model_turn(
                policy=IDLE_FOLLOWUP_ENTRY_POLICY,
                invoke_model=failing_without_tools,
                normalize_stickers=_identity_stickers,
                logger=logger,
                user_id=10,
            )
        )

    tool_log = {
        "type": "tool_result",
        "tool_name": "user_diary",
        "arguments": {"action": "read", "page": 1},
        "result": {"content": "note"},
        "tool_call_id": "call_1",
    }

    async def failing_after_tool(_visible):
        return LoopResult(
            text=PARTIAL_AI_RESPONSE_ERROR_MESSAGE,
            tool_logs=[tool_log],
            status=LoopStatus.PARTIAL_FAILURE,
            turn_messages=[
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "name": "user_diary",
                    "content": '{"content": "note"}',
                }
            ],
        )

    suppressed_idle_turn = asyncio.run(
        prepare_model_turn(
            policy=IDLE_FOLLOWUP_ENTRY_POLICY,
            invoke_model=failing_after_tool,
            normalize_stickers=_identity_stickers,
            logger=logger,
            user_id=10,
        )
    )
    assert suppressed_idle_turn.text == ""
    assert suppressed_idle_turn.runtime_error == "partial_ai_response_failed"
    assert len(suppressed_idle_turn.tool_record_entries) == 1
    assert suppressed_idle_turn.reply_entries == []


def test_prepare_model_turn_gates_completed_clear_to_user_entry() -> None:
    clear_log = {
        "type": "tool_result",
        "tool_name": "execute_telegram_command",
        "arguments": {"command": "/clear"},
        "result": {"success": True},
        "tool_call_id": "call_clear",
    }

    async def model_with_clear(_visible):
        return LoopResult(text="已清空", tool_logs=[clear_log])

    user_prepared = asyncio.run(
        prepare_model_turn(
            policy=USER_ENTRY_POLICY,
            invoke_model=model_with_clear,
            normalize_stickers=_identity_stickers,
            logger=logger,
        )
    )
    schedule_prepared = asyncio.run(
        prepare_model_turn(
            policy=SCHEDULE_ENTRY_POLICY,
            invoke_model=model_with_clear,
            normalize_stickers=_identity_stickers,
            logger=logger,
        )
    )
    assert user_prepared.completed_clear is True
    assert schedule_prepared.completed_clear is False


def test_execute_post_input_turn_orders_stages_and_honors_gate() -> None:
    events: list[str] = []

    async def model_step():
        return await prepare_model_turn(
            policy=SCHEDULE_ENTRY_POLICY,
            invoke_model=lambda _visible: asyncio.sleep(0, result=LoopResult(text="hi", tool_logs=[])),
            normalize_stickers=_identity_stickers,
            logger=logger,
        )

    async def wrap_stage(stage: PostInputStage, step):
        events.append(f"enter:{stage.value}")
        result = await step()
        events.append(f"exit:{stage.value}")
        return result

    async def persist(_prepared):
        events.append("persist")

    async def deliver(_prepared):
        events.append("deliver")
        return "ok"

    async def finalize(_prepared, delivery_result):
        events.append(f"finalize:{delivery_result}")

    outcome = asyncio.run(
        execute_post_input_turn(
            policy=SCHEDULE_ENTRY_POLICY,
            run_model_step=model_step,
            before_persist_and_deliver=lambda _p: asyncio.sleep(0, result=True),
            persist_output=persist,
            deliver_output=deliver,
            finalize_turn=finalize,
            wrap_stage=wrap_stage,
        )
    )
    assert outcome.delivered is True
    assert outcome.delivery_result == "ok"
    assert events == [
        "enter:model",
        "exit:model",
        "enter:history_out",
        "persist",
        "exit:history_out",
        "enter:delivery",
        "deliver",
        "exit:delivery",
        "enter:finalize",
        "finalize:ok",
        "exit:finalize",
    ]

    events.clear()
    gated = asyncio.run(
        execute_post_input_turn(
            policy=IDLE_FOLLOWUP_ENTRY_POLICY,
            run_model_step=model_step,
            before_persist_and_deliver=lambda _p: asyncio.sleep(0, result=False),
            persist_output=persist,
            deliver_output=deliver,
            finalize_turn=finalize,
            wrap_stage=wrap_stage,
        )
    )
    assert gated.delivered is False
    assert events == ["enter:model", "exit:model"]


def test_scheduler_post_input_pipeline_runs_stages_without_zero_balance_suspend(monkeypatch) -> None:
    events: list[tuple] = []
    claim = scheduler.ScheduleClaim(
        schedule_id=99,
        user_id=321,
        run_at=datetime(2026, 7, 29, 12, 0, 0),
        created_at=datetime(2026, 7, 29, 11, 0, 0),
        trigger_reason="check in",
        context_text="",
        instruction="say hi",
        recurrence_unit="none",
        recurrence_interval=1,
        token="b" * 32,
        attempt=1,
    )

    async def fake_user_state(_uid):
        return "<user_state coins=\"5\" />"

    async def fake_coins(_uid):
        return 5

    async def fake_begin_gen(_run):
        events.append(("begin_generation",))
        return True

    async def fake_begin_del(_run):
        events.append(("begin_delivery",))

    async def fake_complete(_run):
        events.append(("complete_claim",))

    async def fake_write_history(uid, records, **kwargs):
        assert "suspend_if_zero" not in kwargs
        events.append(("write_history", uid, list(records)))

    async def fake_history(_uid):
        return [{"role": "user", "content": "scheduled"}]

    async def fake_ai(*args, **kwargs):
        events.append(("ai", kwargs["visible_content_handler"] is not None))
        return LoopResult(text="喝水时间到", tool_logs=[])

    async def fake_normalize(text, *, logger):
        return text

    async def fake_send_reply(**kwargs):
        events.append(("send_private_reply", kwargs["text"]))
        return [SimpleNamespace(message_id=1)]

    async def fake_send_media(**kwargs):
        events.append(("send_generated_media",))
        return []

    async def fake_typing(**kwargs):
        events.append(("typing", kwargs["chat_id"]))

    async def fake_send_message(*args, **kwargs):
        return None

    monkeypatch.setattr(scheduler, "build_user_state_prompt", fake_user_state)
    monkeypatch.setattr(scheduler.process_user, "async_get_user_coins", fake_coins)
    monkeypatch.setattr(scheduler, "_begin_generation", fake_begin_gen)
    monkeypatch.setattr(scheduler, "_begin_delivery", fake_begin_del)
    monkeypatch.setattr(scheduler, "_complete_claim", fake_complete)
    monkeypatch.setattr(scheduler, "write_history", fake_write_history)
    monkeypatch.setattr(scheduler.mysql_connection, "async_get_chat_history", fake_history)
    monkeypatch.setattr(scheduler.ai_chat, "get_ai_response", fake_ai)
    monkeypatch.setattr(scheduler, "normalize_sticker_directives", fake_normalize)
    monkeypatch.setattr(scheduler, "send_private_reply", fake_send_reply)
    monkeypatch.setattr(scheduler, "send_generated_media", fake_send_media)

    bot = SimpleNamespace(send_chat_action=fake_typing, send_message=fake_send_message)
    run = scheduler._ScheduleRun(claim)
    asyncio.run(scheduler._process_schedule_task_locked(run, SimpleNamespace(bot=bot)))

    names = [item[0] for item in events]
    assert names == [
        "begin_generation",
        "write_history",  # scheduled user event
        "typing",
        "ai",
        "begin_delivery",
        "write_history",  # tool entries (empty -> no-op in real write_history)
        "write_history",  # assistant reply
        "send_private_reply",
        "send_generated_media",
        "complete_claim",
    ]


def test_idle_followup_stale_claim_after_tools_persists_only_tool_history(monkeypatch) -> None:
    claim = idle_followup.IdleFollowupClaim(user_id=555, activity_version=2, retry_count=0)
    current_states = iter([True, True, False])  # initial, after recap, after main model
    persisted: list[tuple] = []
    stages: list[str] = []
    sent: list[tuple] = []
    fired: list[tuple] = []
    tool_logs = [
        {
            "type": "tool_result",
            "tool_name": "update_impression",
            "arguments": {"impression": "busy"},
            "result": {"ok": True},
            "tool_call_id": "call_imp",
        }
    ]
    turn_messages = [
        {
            "role": "tool",
            "tool_call_id": "call_imp",
            "name": "update_impression",
            "content": '{"ok": true}',
        }
    ]

    monkeypatch.setattr(
        idle_followup,
        "_claim_is_current",
        lambda _claim: asyncio.sleep(0, result=next(current_states)),
    )
    monkeypatch.setattr(
        idle_followup,
        "_get_followup_user_total_coins",
        lambda _uid: asyncio.sleep(0, result=3),
    )
    monkeypatch.setattr(
        idle_followup.mysql_connection,
        "async_get_chat_history",
        lambda _uid: asyncio.sleep(0, result=[{"role": "user", "content": "hi"}]),
    )
    monkeypatch.setattr(
        idle_followup,
        "_load_recap_memory_context",
        lambda _uid: asyncio.sleep(0, result={"impression": "", "diary_index": []}),
    )
    monkeypatch.setattr(
        idle_followup,
        "_generate_recap",
        lambda *_a: asyncio.sleep(
            0,
            result={
                "recap": "hi",
                "open_loops": "",
                "suggested_follow_up": "",
                "memory_suggestion": {"impression": "", "diary": ""},
            },
        ),
    )
    monkeypatch.setattr(
        idle_followup,
        "build_user_state_prompt",
        lambda _uid: asyncio.sleep(0, result="<user_state />"),
    )
    monkeypatch.setattr(
        idle_followup,
        "_enter_stage",
        lambda _run, stage: asyncio.sleep(0, result=stages.append(stage)),
    )
    monkeypatch.setattr(
        idle_followup.ai_chat,
        "get_ai_response",
        lambda *_a, **_kw: asyncio.sleep(
            0,
            result=LoopResult(text="不该发出", tool_logs=tool_logs, turn_messages=turn_messages),
        ),
    )
    monkeypatch.setattr(
        idle_followup,
        "normalize_sticker_directives",
        lambda text, *, logger: asyncio.sleep(0, result=text),
    )
    monkeypatch.setattr(
        idle_followup,
        "_persist_completed_turn",
        lambda *args, **kwargs: asyncio.sleep(0, result=persisted.append((args, kwargs))),
    )
    monkeypatch.setattr(
        idle_followup,
        "_send_followup_outputs",
        lambda *args: asyncio.sleep(0, result=sent.append(args)),
    )
    monkeypatch.setattr(
        idle_followup,
        "_mark_claim_fired",
        lambda *args, **kwargs: asyncio.sleep(0, result=fired.append((args, kwargs))),
    )

    asyncio.run(
        idle_followup._process_claim(claim, SimpleNamespace(bot=SimpleNamespace()))
    )

    assert stages == ["generating"]  # 未进入 delivering
    assert len(persisted) == 1
    assert persisted[0][0][2] == ""  # assistant_message 被置空
    assert persisted[0][0][3] == turn_messages
    assert sent == []
    assert fired == []
