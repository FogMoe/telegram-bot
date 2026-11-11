"""AI 模型调用与外部计费工具的开销计量测试（§7）。"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from types import SimpleNamespace
from typing import Any

import pytest

from fogmoe_telegram_bot.core import config, metrics
from fogmoe_telegram_bot.features.ai import litellm_client, summary, task_runner
from fogmoe_telegram_bot.features.ai.tool_loop_execution import ToolArguments, execute_tool_call
from fogmoe_telegram_bot.features.ai.usage import (
    UsageAccumulator,
    ai_usage_scope,
    estimate_call_cost_usd,
    extract_token_counts,
    parse_model_pricing,
    record_completion_usage,
    record_tool_execution_usage,
)
from fogmoe_telegram_bot.features.conversation import billing, turn
from fogmoe_telegram_bot.features.conversation.turn_services import default_services
from fogmoe_telegram_bot.features.conversation.turn_types import (
    ChatRef,
    ConversationSettings,
    IncomingMessage,
    ModelResponse,
    SenderRef,
    TurnRequest,
    UserStateRecord,
)
from fogmoe_telegram_bot.features.moderation.spam_ai import judge


def _fake_response(
    *,
    prompt_tokens: int = 120,
    cached_tokens: int = 0,
    cache_read_input_tokens: int = 0,
    completion_tokens: int = 30,
    response_cost: float | None = None,
) -> Any:
    details = SimpleNamespace(cached_tokens=cached_tokens) if cached_tokens else None
    usage = SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        prompt_tokens_details=details,
        cache_read_input_tokens=cache_read_input_tokens or None,
    )
    hidden = {"response_cost": response_cost} if response_cost is not None else {}
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=None))],
        usage=usage,
        _hidden_params=hidden,
    )


def test_extract_token_counts_supports_cached_details_and_clamps():
    resp = _fake_response(prompt_tokens=1000, cached_tokens=400, completion_tokens=250)
    assert extract_token_counts(resp) == (1000, 400, 250, 1250)

    anthropic_style = _fake_response(
        prompt_tokens=500,
        cache_read_input_tokens=800,  # 异常返回超出 prompt_tokens 时截断到 prompt_tokens
        completion_tokens=100,
    )
    assert extract_token_counts(anthropic_style) == (500, 500, 100, 600)

    no_usage = SimpleNamespace(choices=[])
    assert extract_token_counts(no_usage) == (0, 0, 0, 0)


def test_estimate_call_cost_prefers_custom_pricing_then_litellm_then_zero(monkeypatch):
    pricing_json = json.dumps(
        {
            "fogmoe/gpt-5.4": {
                "input": 2.0,
                "cached_input": 0.5,
                "output": 8.0,
            }
        }
    )
    rates = parse_model_pricing(pricing_json)
    assert "fogmoe/gpt-5.4" in rates

    settings = config.AppSettings.from_values(AI_MODEL_PRICING=pricing_json)
    resp_with_hidden = _fake_response(
        prompt_tokens=1_000_000,
        cached_tokens=400_000,
        completion_tokens=250_000,
        response_cost=99.0,
    )
    # 自定义价格优先：(600k * 2.0 + 400k * 0.5 + 250k * 8.0) / 1M = 1.2 + 0.2 + 2.0 = 3.4
    custom_cost = estimate_call_cost_usd(
        provider="fogmoe",
        model="gpt-5.4",
        litellm_model="openai/gpt-5.4",
        response=resp_with_hidden,
        prompt_tokens=1_000_000,
        cached_tokens=400_000,
        completion_tokens=250_000,
        settings=settings,
    )
    assert custom_cost == pytest.approx(3.4)

    # 无自定义配置时读取 _hidden_params["response_cost"]
    empty_settings = config.AppSettings.from_values(AI_MODEL_PRICING="")
    hidden_cost = estimate_call_cost_usd(
        provider="openai",
        model="gpt-4o",
        litellm_model="openai/gpt-4o",
        response=resp_with_hidden,
        prompt_tokens=1000,
        cached_tokens=0,
        completion_tokens=100,
        settings=empty_settings,
    )
    assert hidden_cost == pytest.approx(99.0)

    # _hidden_params 没有成本时回退 litellm.completion_cost；未知模型抛异常时返回 0.0
    resp_unknown = _fake_response(prompt_tokens=500, completion_tokens=100)
    monkeypatch.setattr(
        litellm_client.litellm,
        "completion_cost",
        lambda **kwargs: (_ for _ in ()).throw(ValueError("unknown model")),
    )
    unknown_cost = estimate_call_cost_usd(
        provider="fogmoe",
        model="custom-local-model",
        litellm_model="openai/custom-local-model",
        response=resp_unknown,
        prompt_tokens=500,
        cached_tokens=0,
        completion_tokens=100,
        settings=empty_settings,
    )
    assert unknown_cost == 0.0


def test_litellm_and_tools_record_bounded_metrics_and_turn_accumulator(monkeypatch):
    metrics.REGISTRY.reset()

    async def fake_acompletion(**kwargs):
        return _fake_response(
            prompt_tokens=200,
            cached_tokens=50,
            completion_tokens=40,
            response_cost=0.0015,
        )

    monkeypatch.setattr(litellm_client.litellm, "acompletion", fake_acompletion)
    monkeypatch.setattr(
        config,
        "OPENAI_API_KEY",
        "sk-test",
    )
    monkeypatch.setattr(config, "AI_VISION_PROVIDER", "openai")
    monkeypatch.setattr(config, "OPENAI_VISION_MODEL", "gpt-4o-mini")

    accumulator = UsageAccumulator()

    async def run_scenario():
        with ai_usage_scope(entry="user", accumulator=accumulator, clear_accumulator=True):
            # 1. 同轮内的 vision 子任务自动打上 task="vision"、entry="user"
            await task_runner.run_ai_task(
                "vision",
                messages=[{"role": "user", "content": "describe"}],
            )
            # 2. 主聊天模型调用打上 task="chat"、entry="user"
            with ai_usage_scope(task="chat"):
                await litellm_client.create_chat_completion(
                    "openai",
                    "gpt-4o",
                    [{"role": "user", "content": "hello"}],
                )
                # 3. 外部计费工具成功 + 免费工具成功 + 外部计费工具返回 error
                await execute_tool_call(
                    "google_search",
                    ToolArguments({"query": "weather"}, {"query": "weather"}, None),
                    handlers={"google_search": lambda query: {"results": [query]}},
                    available_tool_names={"google_search"},
                    provider_name="OpenAI",
                )
                await execute_tool_call(
                    "get_current_beijing_time",
                    ToolArguments({}, {}, None),
                    handlers={"get_current_beijing_time": lambda: {"time": "12:00"}},
                    available_tool_names={"get_current_beijing_time"},
                    provider_name="OpenAI",
                )
                await execute_tool_call(
                    "generate_image",
                    ToolArguments({"prompt": "cat"}, {"prompt": "cat"}, None),
                    handlers={"generate_image": lambda prompt: {"error": "upstream down"}},
                    available_tool_names={"generate_image"},
                    provider_name="OpenAI",
                )

    asyncio.run(run_scenario())

    snap = accumulator.snapshot()
    assert snap.ai_calls == 2
    assert snap.prompt_tokens == 400
    assert snap.cached_tokens == 100
    assert snap.completion_tokens == 80
    assert snap.total_tokens == 480
    assert snap.cost_usd == pytest.approx(0.003)
    assert snap.tool_calls == 3
    assert snap.billed_tool_calls == 1

    ms = metrics.snapshot()
    assert ms.counter("ai.calls", provider="openai", task="vision", entry="user") == 1
    assert ms.counter("ai.calls", provider="openai", task="chat", entry="user") == 1
    assert ms.counter("ai.input_tokens", provider="openai", task="chat", entry="user") == 200
    assert ms.counter("ai.cached_input_tokens", provider="openai", task="chat", entry="user") == 50
    assert ms.counter("ai.output_tokens", provider="openai", task="chat", entry="user") == 40
    assert ms.counter("ai.cost_microusd", provider="openai", task="chat", entry="user") == 1500
    assert ms.counter("tool.billed_calls", tool="google_search", category="search") == 1
    assert ms.counter("tool.billed_calls", tool="generate_image", category="image_gen") == 0

    # 确保所有 ai.* 计数器标签只含有限集合键，绝不包含 user_id
    for (metric_name, label_pairs) in ms.counters:
        if metric_name.startswith("ai."):
            label_names = {k for k, _ in label_pairs}
            assert "user_id" not in label_names
            assert label_names <= {"provider", "task", "entry", "status"}


def test_background_summary_isolates_usage_context_from_parent_turn(monkeypatch, caplog):
    metrics.REGISTRY.reset()
    caplog.set_level(logging.INFO)
    parent_accumulator = UsageAccumulator()

    async def fake_generate_and_store_summary(user_id: int) -> str:
        record_completion_usage(
            "gemini",
            "gemini-3-flash-preview",
            litellm_model="gemini/gemini-3-flash-preview",
            response=_fake_response(prompt_tokens=500, completion_tokens=120, response_cost=0.0004),
        )
        return "归档摘要"

    async def fake_update_latest(user_id: int, text: str) -> None:
        return None

    monkeypatch.setattr(summary, "_generate_and_store_summary", fake_generate_and_store_summary)
    monkeypatch.setattr(
        summary.mysql_connection,
        "async_update_latest_history_state_summary",
        fake_update_latest,
    )

    async def run_scenario():
        with ai_usage_scope(
            entry="user",
            task="chat",
            accumulator=parent_accumulator,
            clear_accumulator=True,
        ):
            record_completion_usage(
                "openai",
                "gpt-4o",
                litellm_model="openai/gpt-4o",
                response=_fake_response(prompt_tokens=100, completion_tokens=20, response_cost=0.001),
            )
            # 模拟历史溢出时在用户轮次内触发后台摘要任务
            task = asyncio.create_task(summary._process_summary_for_user(42))
            await task

    asyncio.run(run_scenario())

    # 父轮次的累加器只记了主聊天的 1 次调用，不被后台摘要污染
    parent_snap = parent_accumulator.snapshot()
    assert parent_snap.ai_calls == 1
    assert parent_snap.prompt_tokens == 100

    # 后台摘要自身按 entry="background", task="summary" 记入全局指标
    ms = metrics.snapshot()
    assert ms.counter("ai.calls", provider="gemini", task="summary", entry="background") == 1

    # 后台摘要用自己的累加器，完成日志带上本次的调用数、token 与成本
    finished = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("background summary finished")
    ]
    assert len(finished) == 1
    assert "user_id=42 outcome=stored" in finished[0]
    assert "ai_calls=1 in_tokens=500" in finished[0]
    assert "out_tokens=120 cost_usd=0.000400" in finished[0]


def test_parse_model_pricing_rejects_non_finite_rates():
    rates = parse_model_pricing(
        json.dumps(
            {
                "openai/gpt-4o": {"input": "NaN", "output": 1},
                "openai/gpt-4o-mini": {"input": 1, "output": "Infinity"},
                "openai/o3": {"input": 1, "output": 2, "cached_input": "-Infinity"},
                "openai/gpt-5": {"input": 1.25, "output": 10, "cached_input": 0.125},
            }
        )
    )
    assert set(rates) == {"openai/gpt-5"}


def test_create_chat_completion_keeps_response_when_metering_fails(monkeypatch, caplog):
    response = _fake_response()

    async def fake_acompletion(**kwargs):
        return response

    def broken_metering(*args, **kwargs):
        raise ValueError("cannot convert float NaN to integer")

    monkeypatch.setattr(litellm_client.litellm, "acompletion", fake_acompletion)
    monkeypatch.setattr(litellm_client, "record_completion_usage", broken_metering)
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-test")

    result = asyncio.run(
        litellm_client.create_chat_completion(
            "openai",
            "gpt-4o",
            [{"role": "user", "content": "hello"}],
        )
    )

    assert result is response
    assert any("Failed to record AI usage" in record.getMessage() for record in caplog.records)


def test_run_turn_logs_charged_coins_with_usage_summary(caplog):
    metrics.REGISTRY.reset()

    async def fake_run_model(request):
        record_completion_usage(
            "openai",
            "gpt-4o",
            litellm_model="openai/gpt-4o",
            response=_fake_response(
                prompt_tokens=300,
                cached_tokens=100,
                completion_tokens=60,
                response_cost=0.0025,
            ),
        )
        record_tool_execution_usage("google_search", failed=False)
        return ModelResponse(text="你好喵", tool_logs=[])

    async def noop_async(*args, **kwargs):
        return None

    async def fake_insert_history(*args, **kwargs):
        return False, None, []

    async def fake_load_history(*args, **kwargs):
        return [{"role": "user", "content": "hi"}]

    async def fake_send_reply(*args, **kwargs):
        return [SimpleNamespace(message_id=99)]

    services = dataclasses.replace(
        default_services(),
        charge=lambda *a, **kw: asyncio.sleep(
            0,
            result=billing.TurnCharge(
                status=billing.TurnChargeStatus.CHARGED,
                total_cost=2,
            ),
        ),
        flush_events=noop_async,
        load_user_state=lambda *a, **kw: asyncio.sleep(
            0,
            result=UserStateRecord(impression="熟人", diary_exists=False),
        ),
        insert_records=fake_insert_history,
        insert_record=fake_insert_history,
        get_history=fake_load_history,
        schedule_summary=lambda *a, **kw: None,
        handle_history_overflow=noop_async,
        arm_idle_followup=noop_async,
        archive_completed_clear=noop_async,
        analyze_image=lambda *a, **kw: asyncio.sleep(0, result=""),
        run_model=fake_run_model,
        reply_text=noop_async,
        send_typing=noop_async,
        send_warning=noop_async,
        send_archive=noop_async,
        normalize_stickers=lambda text: asyncio.sleep(0, result=text),
        send_reply=fake_send_reply,
        send_generated_media=lambda *a, **kw: asyncio.sleep(0, result=[]),
        log_group_message=noop_async,
    )
    msg = SimpleNamespace(
        message_id=10,
        text="你好",
        caption=None,
        photo=None,
        sticker=None,
        animation=None,
        document=None,
        date=None,
        edit_date=None,
        reply_to_message=None,
        quote=None,
        reply_text=noop_async,
        from_user=SimpleNamespace(id=1001, username="alice", first_name="Alice"),
    )
    req = TurnRequest(
        chat=ChatRef(chat_id=1001, chat_type="private"),
        sender=SenderRef(user_id=1001, username="alice"),
        messages=(IncomingMessage(message=msg),),
        bot=SimpleNamespace(send_message=noop_async),
    )

    with caplog.at_level(logging.INFO, logger="fogmoe_telegram_bot.features.conversation.turn"):
        result = asyncio.run(turn.run_turn(req, services, ConversationSettings()))

    assert result.usage.ai_calls == 1
    assert result.usage.prompt_tokens == 300
    assert result.usage.cached_tokens == 100
    assert result.usage.completion_tokens == 60
    assert result.usage.billed_tool_calls == 1
    log_text = "\n".join(caplog.messages)
    assert "charged_coins=2" in log_text
    assert "ai_calls=1 in_tokens=300 cached_tokens=100 out_tokens=60" in log_text
    assert "cost_usd=0.002500 tool_calls=1 billed_tool_calls=1" in log_text


def test_typesafe_judge_records_call_and_input_token_metrics():
    metrics.REGISTRY.reset()

    class FakeResponse:
        def __init__(self, status: int, body: dict[str, Any]) -> None:
            self.status = status
            self._body = body

        async def __aenter__(self) -> FakeResponse:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def json(self, *, content_type: str | None = None) -> dict[str, Any]:
            return self._body

    class FakeSession:
        def __init__(self, response: FakeResponse) -> None:
            self._response = response

        def post(self, url: str, *, json: dict[str, Any]) -> FakeResponse:
            return self._response

    sample = judge.MessageForReview(text="普通聊天", is_caption=False, sender_name="Alice")
    ok_session = FakeSession(
        FakeResponse(
            200,
            {
                "model": "jev-1.13.0",
                "answers": {"spam": {"noul": 0.05}},
                "usage": {"input_tokens": 185},
            },
        )
    )
    judgment = asyncio.run(judge.request_judgment(ok_session, sample))  # type: ignore[arg-type]
    assert judgment.input_tokens == 185

    err_session = FakeSession(FakeResponse(503, {}))
    with pytest.raises(judge.JudgeError):
        asyncio.run(judge.request_judgment(err_session, sample))  # type: ignore[arg-type]

    ms = metrics.snapshot()
    assert ms.counter("ai.typesafe_calls", status="ok") == 1
    assert ms.counter("ai.typesafe_calls", status="error") == 1
    assert ms.counter("ai.typesafe_input_tokens") == 185
