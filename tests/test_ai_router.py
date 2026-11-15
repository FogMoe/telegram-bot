import asyncio
from types import SimpleNamespace

import pytest

from fogmoe_telegram_bot.core import ai_providers, config
from fogmoe_telegram_bot.core.deadline import Deadline
from fogmoe_telegram_bot.features.ai import router, tool_runner
from fogmoe_telegram_bot.features.ai.context_budget import ContextBudgetExceededError
from fogmoe_telegram_bot.features.ai.errors import SafetyBlockError
from fogmoe_telegram_bot.features.ai.tools.context import get_tool_request_context
from fogmoe_telegram_bot.features.ai.types import (
    JobAbortedError,
    LoopResult,
    LoopStatus,
    MediaDeliveryState,
    PartialAIResponseError,
    ToolCallRecord,
    ToolCallStatus,
    TurnDeadlineError,
)


def _record(tool_name, **kwargs):
    return ToolCallRecord(
        tool_name=tool_name,
        tool_call_id=f"call_{tool_name}",
        arguments={},
        status=ToolCallStatus.COMPLETED,
        **kwargs,
    )


@pytest.fixture(autouse=True)
def clear_provider_circuit_state():
    router.CHAT_CIRCUIT.reset()
    yield
    router.CHAT_CIRCUIT.reset()


def use_chat_services(monkeypatch, services):
    """`services` 的键顺序就是 AI_CHAT_ORDER，每个 provider 配一个聊天模型；工具循环换成按 provider 分派的替身。

    替身是普通函数，由这里包成 async；整轮截止时间不交给替身，记在返回值的 `deadlines` 里。
    """
    monkeypatch.setattr(config, "AI_SERVICE_ORDER", list(services))
    for name in services:
        spec = ai_providers.require(name)
        monkeypatch.setattr(config, spec.model_key("chat"), f"{name}-chat")
        fallback_key = spec.fallback_model_key("chat")
        if fallback_key:
            monkeypatch.setattr(config, fallback_key, None)
    deadlines = []

    async def fake_run_tool_loop(provider, model, messages, tool_context, **kwargs):
        deadlines.append(kwargs.get("deadline"))
        result = services[provider](
            messages,
            get_tool_request_context().get("user_id"),
            tool_context=tool_context,
            visible_content_handler=kwargs.get("visible_content_handler"),
        )
        if asyncio.iscoroutine(result):
            result = await result
        return result

    monkeypatch.setattr(router, "run_tool_loop", fake_run_tool_loop)
    return deadlines


@pytest.fixture
def chat_loop(monkeypatch):
    """替换主聊天的工具循环：记录每次调用，按 `behaviour` 返回或抛错。"""
    calls = []
    behaviour = SimpleNamespace(handler=lambda provider, model: LoopResult("ok"))

    async def fake_run_tool_loop(provider, model, messages, tool_context, **kwargs):
        calls.append(
            {
                "provider": provider,
                "model": model,
                "messages": messages,
                "tool_context": tool_context,
                "kwargs": kwargs,
            }
        )
        return behaviour.handler(provider, model)

    monkeypatch.setattr(router, "run_tool_loop", fake_run_tool_loop)
    return SimpleNamespace(calls=calls, behaviour=behaviour)


def test_get_ai_response_retries_image_messages_as_text(monkeypatch):
    image_messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "describe this image"},
                {
                    "type": "image_url",
                    "image_url": {"url": "https://example.test/a.png"},
                },
            ],
        }
    ]
    calls = []

    async def fake_try_ai_services(
        messages,
        user_id,
        tool_context=None,
        visible_content_handler=None,
        text_fallback_messages=None,
        deadline=None,
    ):
        calls.append(messages)
        if len(calls) == 1:
            return None, RuntimeError("provider failed")
        return LoopResult("text fallback response"), None

    monkeypatch.setattr(router, "_try_ai_services", fake_try_ai_services)

    response = asyncio.run(router.get_ai_response(image_messages, user_id=123))

    assert response == LoopResult("text fallback response")
    assert calls == [
        image_messages,
        [{"role": "user", "content": "describe this image"}],
    ]


def test_delivered_to_user_counts_visible_text_and_sent_media():
    assert not LoopResult("", tool_calls=(_record("google_search"),)).delivered_to_user
    assert not LoopResult("", delivered_contents=("  ",)).delivered_to_user
    assert LoopResult("", delivered_contents=("我查一下",)).delivered_to_user
    for state in (MediaDeliveryState.SENT, MediaDeliveryState.UNKNOWN):
        sent_media = _record("generate_image", media_delivery=state)
        assert LoopResult("", tool_calls=(sent_media,)).delivered_to_user
    pending_media = _record("generate_image", media_delivery=MediaDeliveryState.PENDING)
    assert not LoopResult("", tool_calls=(pending_media,)).delivered_to_user


def test_a_failure_after_media_was_sent_needs_no_error_notice(monkeypatch):
    sent_media = _record("generate_image", media_delivery=MediaDeliveryState.SENT)

    def failing_service(messages, user_id, tool_context=None, visible_content_handler=None):
        raise PartialAIResponseError("follow-up failed", LoopResult("", tool_calls=(sent_media,)))

    use_chat_services(monkeypatch, {"gemini": failing_service})

    response = asyncio.run(router.get_ai_response([], user_id=123))
    assert response == LoopResult(
        "",
        status=LoopStatus.PARTIAL_FAILURE,
        tool_calls=(sent_media,),
    )


def test_a_failure_after_tools_never_reruns_them(settings_override, monkeypatch):
    settings_override(
        AI_CHAT_ORDER="gemini,openai",
        GEMINI_CHAT_MODEL="primary",
        GEMINI_CHAT_FALLBACK_MODEL="fallback",
        OPENAI_CHAT_MODEL="gpt",
    )
    impression_call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "update_impression", "arguments": '{"impression": "likes tea"}'},
    }
    responses = [
        SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="", tool_calls=[impression_call]))]
        ),
        SimpleNamespace(choices=[]),
    ]
    model_calls = []
    tool_runs = []

    async def fake_completion(provider, model, *, messages, **kwargs):
        model_calls.append((provider, model))
        return responses.pop(0)

    monkeypatch.setattr(tool_runner, "create_chat_completion", fake_completion)
    monkeypatch.setitem(
        tool_runner.AI_TOOL_HANDLERS,
        "update_impression",
        lambda **kwargs: tool_runs.append(kwargs) or {"status": "updated"},
    )

    response = asyncio.run(
        router.get_ai_response([{"role": "user", "content": "remember"}], user_id=123)
    )

    assert response.text == router.PARTIAL_AI_RESPONSE_ERROR_MESSAGE
    assert tool_runs == [{"impression": "likes tea"}]
    # 同一个模型调用了两次（工具前、工具后），备用模型与下一个 provider 都没有被调用。
    assert model_calls == [("gemini", "primary"), ("gemini", "primary")]
    assert [(record.tool_name, record.status) for record in response.tool_calls] == [
        ("update_impression", ToolCallStatus.COMPLETED)
    ]


def test_text_only_chat_provider_uses_vision_text_fallback_messages(monkeypatch):
    image_messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "runtime message without description"},
                {
                    "type": "image_url",
                    "image_url": {"url": "https://example.test/a.png"},
                },
            ],
        }
    ]
    text_fallback_messages = [
        {
            "role": "user",
            "content": (
                "<metadata><media type=\"photo\">"
                "<description>a cat on a desk</description>"
                "</media></metadata><message>[photo]</message>"
            ),
        }
    ]
    calls = []

    def fake_service(
        messages,
        user_id,
        tool_context=None,
        visible_content_handler=None,
    ):
        calls.append(messages)
        return LoopResult("ok")

    use_chat_services(
        monkeypatch,
        {"siliconflow": fake_service},
    )
    monkeypatch.setattr(
        router,
        "chat_service_supports_vision",
        lambda service_name: False,
    )
    monkeypatch.setattr(
        router,
        "chat_model_for_service",
        lambda service_name: "deepseek-ai/DeepSeek-V4-Flash",
    )

    response = asyncio.run(
        router.get_ai_response(
            image_messages,
            user_id=123,
            text_fallback_messages=text_fallback_messages,
        )
    )

    assert response == LoopResult("ok")
    assert calls == [text_fallback_messages]


def test_vision_capable_chat_provider_keeps_multimodal_messages(monkeypatch):
    image_messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "describe this image"},
                {
                    "type": "image_url",
                    "image_url": {"url": "https://example.test/a.png"},
                },
            ],
        }
    ]
    calls = []

    def fake_service(
        messages,
        user_id,
        tool_context=None,
        visible_content_handler=None,
    ):
        calls.append(messages)
        return LoopResult("ok")

    use_chat_services(monkeypatch, {"openai": fake_service})
    monkeypatch.setattr(router, "chat_service_supports_vision", lambda service_name: True)

    response = asyncio.run(router.get_ai_response(image_messages, user_id=123))

    assert response == LoopResult("ok")
    assert calls == [image_messages]


def test_chat_circuit_uses_the_router_thresholds():
    assert router.CHAT_CIRCUIT.failure_threshold == router.AI_PROVIDER_CIRCUIT_FAILURE_THRESHOLD
    assert router.CHAT_CIRCUIT.window_seconds == router.AI_PROVIDER_CIRCUIT_WINDOW_SECONDS
    assert router.CHAT_CIRCUIT.cooldown_seconds == router.AI_PROVIDER_CIRCUIT_COOLDOWN_SECONDS


def test_open_provider_circuit_skips_to_next_service(monkeypatch):
    calls = []

    def failing_service(
        messages,
        user_id,
        tool_context=None,
        visible_content_handler=None,
    ):
        calls.append("gemini")
        raise AssertionError("open circuit provider should be skipped")

    def fallback_service(
        messages,
        user_id,
        tool_context=None,
        visible_content_handler=None,
    ):
        calls.append("siliconflow")
        return LoopResult("ok")

    use_chat_services(
        monkeypatch,
        {
            "gemini": failing_service,
            "siliconflow": fallback_service,
        },
    )
    monkeypatch.setattr(
        router.CHAT_CIRCUIT,
        "is_open",
        lambda provider, now=None: provider == "gemini",
    )

    response = asyncio.run(router.get_ai_response([], user_id=123))

    assert response == LoopResult("ok")
    assert calls == ["siliconflow"]


def test_a_failed_provider_counts_against_its_circuit_and_falls_back(monkeypatch):
    def failing_service(messages, user_id, tool_context=None, visible_content_handler=None):
        raise RuntimeError("openai down")

    def next_service(messages, user_id, tool_context=None, visible_content_handler=None):
        return LoopResult("ok")

    use_chat_services(monkeypatch, {"openai": failing_service, "gemini": next_service})

    assert asyncio.run(router.get_ai_response([], user_id=123)) == LoopResult("ok")
    assert router.CHAT_CIRCUIT.failure_count("openai") == 1
    assert router.CHAT_CIRCUIT.failure_count("gemini") == 0


def test_partial_timeout_logs_warning_without_traceback(monkeypatch, caplog):
    def timed_out_service(
        messages,
        user_id,
        tool_context=None,
        visible_content_handler=None,
    ):
        try:
            raise TimeoutError("chat completion timed out")
        except TimeoutError as exc:
            raise PartialAIResponseError(
                "chat completion timed out",
                LoopResult("", tool_calls=(_record("advisor"),)),
            ) from exc

    use_chat_services(monkeypatch, {"gemini": timed_out_service})

    with caplog.at_level("WARNING"):
        response = asyncio.run(router.get_ai_response([], user_id=123))

    timeout_records = [
        record
        for record in caplog.records
        if "timed out after partial AI response" in record.getMessage()
    ]
    assert response.text == router.PARTIAL_AI_RESPONSE_ERROR_MESSAGE
    assert len(timeout_records) == 1
    assert timeout_records[0].exc_info is None


def test_context_budget_error_stops_provider_fallback(monkeypatch):
    calls = []

    def oversized_service(
        messages,
        user_id,
        tool_context=None,
        visible_content_handler=None,
    ):
        calls.append("openai")
        raise ContextBudgetExceededError(150_001, 150_000)

    def fallback_service(
        messages,
        user_id,
        tool_context=None,
        visible_content_handler=None,
    ):
        calls.append("gemini")
        return LoopResult("unexpected")

    use_chat_services(
        monkeypatch,
        {"openai": oversized_service, "gemini": fallback_service},
    )

    response = asyncio.run(router.get_ai_response([], user_id=123))

    assert calls == ["openai"]
    assert response.text == router.CONTEXT_BUDGET_ERROR_MESSAGE
    assert "当前对话内容太多" in response.text
    assert "精简" not in response.text
    assert "Token" not in response.text
    assert "150,001" not in response.text
    assert response.tool_calls == ()
    assert response.runtime_error == "context_budget_exceeded"
    assert router.CHAT_CIRCUIT.failure_count("openai") == 0


def test_context_budget_error_after_tool_result_preserves_tool_calls(monkeypatch):
    tool_calls = (_record("lookup", public_result="ok"),)

    def oversized_service(
        messages,
        user_id,
        tool_context=None,
        visible_content_handler=None,
    ):
        try:
            raise ContextBudgetExceededError(150_001, 150_000)
        except ContextBudgetExceededError as exc:
            raise PartialAIResponseError(str(exc), LoopResult("", tool_calls=tool_calls)) from exc

    use_chat_services(monkeypatch, {"openai": oversized_service})

    response = asyncio.run(router.get_ai_response([], user_id=123))

    assert response.runtime_error == "context_budget_exceeded"
    assert response.tool_calls == tool_calls


def test_safety_block_falls_through_to_next_service_for_gemini(monkeypatch):
    calls = []

    def blocked_service(messages, user_id, tool_context=None, visible_content_handler=None):
        calls.append("gemini")
        raise SafetyBlockError("blocked by safety")

    def next_service(messages, user_id, tool_context=None, visible_content_handler=None):
        calls.append("openai")
        return LoopResult("ok")

    use_chat_services(monkeypatch, {"gemini": blocked_service, "openai": next_service})

    assert asyncio.run(router.get_ai_response([], user_id=123)) == LoopResult("ok")
    assert calls == ["gemini", "openai"]
    # 安全拦截不是 provider 故障，不计入熔断。
    assert router.CHAT_CIRCUIT.failure_count("gemini") == 0


def test_safety_block_from_other_providers_stops_the_chain_as_a_status(monkeypatch):
    calls = []

    def blocked_service(messages, user_id, tool_context=None, visible_content_handler=None):
        calls.append("openai")
        raise SafetyBlockError("blocked by safety")

    def next_service(messages, user_id, tool_context=None, visible_content_handler=None):
        calls.append("gemini")
        return LoopResult("unexpected")

    use_chat_services(monkeypatch, {"openai": blocked_service, "gemini": next_service})

    response = asyncio.run(router.get_ai_response([], user_id=123))

    # 没有声明安全拦截可以换下一个 provider：回退链停下，以状态而不是异常交给入口。
    assert calls == ["openai"]
    assert response.status is LoopStatus.SAFETY_BLOCKED
    assert response.text == router.AI_SERVICE_ERROR_MESSAGE
    assert response.runtime_error == "ai_safety_blocked"
    assert router.CHAT_CIRCUIT.failure_count("openai") == 0


def test_an_aborted_background_turn_becomes_a_status_without_fallback(monkeypatch):
    calls = []

    def aborted_service(messages, user_id, tool_context=None, visible_content_handler=None):
        calls.append("openai")
        raise JobAbortedError("background job was aborted")

    def next_service(messages, user_id, tool_context=None, visible_content_handler=None):
        calls.append("gemini")
        return LoopResult("unexpected")

    use_chat_services(monkeypatch, {"openai": aborted_service, "gemini": next_service})

    response = asyncio.run(router.get_ai_response([], user_id=123))

    assert calls == ["openai"]
    assert response == LoopResult("", status=LoopStatus.ABORTED)
    assert router.CHAT_CIRCUIT.failure_count("openai") == 0


def test_unknown_provider_in_chat_order_is_skipped(monkeypatch, chat_loop):
    monkeypatch.setattr(config, "AI_SERVICE_ORDER", ["typo-provider", "openai"])
    monkeypatch.setattr(config, "OPENAI_CHAT_MODEL", "openai-chat")
    chat_loop.behaviour.handler = lambda provider, model: LoopResult(f"{provider}:{model}")

    assert asyncio.run(router.get_ai_response([], user_id=123)) == LoopResult("openai:openai-chat")
    assert [call["provider"] for call in chat_loop.calls] == ["openai"]


def test_chat_order_comes_from_the_active_settings(monkeypatch):
    seen = []

    async def failing_then_ok(provider, model, messages, tool_context, **kwargs):
        seen.append(provider)
        if provider == "azure":
            raise RuntimeError("azure down")
        return LoopResult("ok")

    monkeypatch.setattr(router, "run_tool_loop", failing_then_ok)

    with config.override_settings(
        AI_CHAT_ORDER="azure, openai",
        AZURE_OPENAI_CHAT_MODEL="azure-chat",
        OPENAI_CHAT_MODEL="openai-chat",
    ):
        assert asyncio.run(router.get_ai_response([], user_id=123)) == LoopResult("ok")

    assert seen == ["azure", "openai"]


def test_each_provider_runs_the_tool_loop_with_its_declared_chat_model(settings_override, chat_loop):
    settings_override(AI_CHAT_ORDER="fogmoe", FOGMOE_CHAT_MODEL="gpt-5.6-luna")
    messages = [{"role": "user", "content": "hello"}]
    context = {"user_id": 123, "chat_id": 9}

    response = asyncio.run(router.get_ai_response(messages, 123, tool_context=context))

    assert response == LoopResult("ok")
    (call,) = chat_loop.calls
    assert call["provider"] == "fogmoe"
    assert call["model"] == "gpt-5.6-luna"
    assert call["messages"] == messages
    assert call["tool_context"] is context
    assert call["kwargs"]["provider_name"] == "FOGMOE"
    assert "completion_kwargs" not in call["kwargs"]


def test_the_request_context_is_a_per_attempt_copy_with_the_user(settings_override, monkeypatch):
    settings_override(AI_CHAT_ORDER="openai", OPENAI_CHAT_MODEL="gpt")
    seen = {}

    async def fake_run_tool_loop(provider, model, messages, tool_context, **kwargs):
        seen["inside"] = dict(get_tool_request_context())
        return LoopResult("ok")

    monkeypatch.setattr(router, "run_tool_loop", fake_run_tool_loop)

    asyncio.run(router.get_ai_response([], 7, tool_context={"chat_id": 9}))

    assert seen["inside"] == {"chat_id": 9, "user_id": 7}
    assert get_tool_request_context() == {}


def test_visible_handler_and_deadline_reach_the_tool_loop(settings_override, chat_loop):
    settings_override(AI_CHAT_ORDER="openai", OPENAI_CHAT_MODEL="gpt")
    handler = object()
    deadline = Deadline(60)

    asyncio.run(
        router.get_ai_response([], 1, visible_content_handler=handler, deadline=deadline)
    )

    assert chat_loop.calls[0]["kwargs"]["visible_content_handler"] is handler
    assert chat_loop.calls[0]["kwargs"]["deadline"] is deadline


def test_zhipu_resolves_to_zai_and_forwards_declared_skip_tools(
    monkeypatch, settings_override, chat_loop
):
    settings_override(AI_CHAT_ORDER="zhipu", ZHIPU_CHAT_MODEL="glm-chat")

    asyncio.run(router.get_ai_response([], 1))

    (call,) = chat_loop.calls
    assert call["provider"] == "zai"
    assert call["model"] == "glm-chat"
    assert call["kwargs"]["provider_name"] == "Z.ai"
    assert tuple(call["kwargs"]["skip_tools"]) == ()

    from dataclasses import replace
    from types import MappingProxyType

    from fogmoe_telegram_bot.core import ai_providers

    patched = {
        key: (
            replace(spec, skip_tools=("google_search",))
            if spec.name == "zai"
            else spec
        )
        for key, spec in ai_providers._BY_NAME.items()
    }
    monkeypatch.setattr(ai_providers, "_BY_NAME", MappingProxyType(patched))

    asyncio.run(router.get_ai_response([], 1))

    assert tuple(chat_loop.calls[1]["kwargs"]["skip_tools"]) == ("google_search",)


def test_other_providers_hide_no_tools(settings_override, chat_loop):
    settings_override(AI_CHAT_ORDER="openai", OPENAI_CHAT_MODEL="gpt")

    asyncio.run(router.get_ai_response([], 1))

    assert not chat_loop.calls[0]["kwargs"]["skip_tools"]


def test_a_provider_without_a_chat_model_is_skipped(settings_override, chat_loop):
    settings_override(AI_CHAT_ORDER="openai,gemini", GEMINI_CHAT_MODEL="gemini-chat")

    assert asyncio.run(router.get_ai_response([], 1)) == LoopResult("ok")
    assert [call["provider"] for call in chat_loop.calls] == ["gemini"]


def test_gemini_retries_with_its_fallback_model(settings_override, chat_loop):
    settings_override(
        AI_CHAT_ORDER="gemini",
        GEMINI_CHAT_MODEL="primary",
        GEMINI_CHAT_FALLBACK_MODEL="fallback",
    )

    def behave(provider, model):
        if model == "primary":
            raise RuntimeError("primary down")
        return LoopResult(f"from {model}")

    chat_loop.behaviour.handler = behave

    assert asyncio.run(router.get_ai_response([], 1)) == LoopResult("from fallback")
    assert [call["model"] for call in chat_loop.calls] == ["primary", "fallback"]
    # 换了模型就成功了：这个 provider 不计熔断失败。
    assert router.CHAT_CIRCUIT.failure_count("gemini") == 0


def test_gemini_uses_the_fallback_when_the_primary_model_is_not_configured(
    settings_override, chat_loop
):
    settings_override(AI_CHAT_ORDER="gemini", GEMINI_CHAT_FALLBACK_MODEL="fallback")

    asyncio.run(router.get_ai_response([], 1))

    assert [call["model"] for call in chat_loop.calls] == ["fallback"]


@pytest.mark.parametrize(
    ("error", "expected_text"),
    [
        (ContextBudgetExceededError(150_001, 150_000), router.CONTEXT_BUDGET_ERROR_MESSAGE),
        (
            PartialAIResponseError("after tools", LoopResult("", tool_calls=(_record("lookup"),))),
            router.PARTIAL_AI_RESPONSE_ERROR_MESSAGE,
        ),
        (TurnDeadlineError("deadline", "tool", LoopResult("")), router.TURN_DEADLINE_ERROR_MESSAGE),
    ],
)
def test_context_partial_and_deadline_errors_skip_the_fallback_model(
    settings_override, chat_loop, error, expected_text
):
    settings_override(
        AI_CHAT_ORDER="gemini,openai",
        GEMINI_CHAT_MODEL="primary",
        GEMINI_CHAT_FALLBACK_MODEL="fallback",
        OPENAI_CHAT_MODEL="gpt",
    )

    def behave(provider, model):
        raise error

    chat_loop.behaviour.handler = behave

    text = asyncio.run(router.get_ai_response([], 1)).text

    assert text == expected_text
    assert [(call["provider"], call["model"]) for call in chat_loop.calls] == [("gemini", "primary")]


SAFETY_TEXT = "Response was blocked: SAFETY"


def test_gemini_safety_text_falls_through_without_tripping_the_circuit(settings_override, chat_loop):
    settings_override(
        AI_CHAT_ORDER="gemini,openai",
        GEMINI_CHAT_MODEL="primary",
        OPENAI_CHAT_MODEL="gpt",
    )

    def behave(provider, model):
        if provider == "gemini":
            raise RuntimeError(SAFETY_TEXT)
        return LoopResult("from openai")

    chat_loop.behaviour.handler = behave

    assert asyncio.run(router.get_ai_response([], 1)) == LoopResult("from openai")
    assert router.CHAT_CIRCUIT.failure_count("gemini") == 0


def test_other_providers_do_not_translate_safety_text(settings_override, chat_loop):
    settings_override(AI_CHAT_ORDER="openai,gemini", OPENAI_CHAT_MODEL="gpt", GEMINI_CHAT_MODEL="g")

    def behave(provider, model):
        if provider == "openai":
            raise RuntimeError(SAFETY_TEXT)
        return LoopResult("from gemini")

    chat_loop.behaviour.handler = behave

    assert asyncio.run(router.get_ai_response([], 1)) == LoopResult("from gemini")
    # 没有声明安全拦截的 provider：这是一次普通失败。
    assert router.CHAT_CIRCUIT.failure_count("openai") == 1


def test_a_sandbox_cleanup_failure_does_not_rerun_a_finished_turn(
    settings_override, chat_loop, monkeypatch
):
    settings_override(AI_CHAT_ORDER="openai,gemini", OPENAI_CHAT_MODEL="gpt", GEMINI_CHAT_MODEL="g")

    async def broken_cleanup():
        raise RuntimeError("sandbox API down")

    monkeypatch.setattr(router, "cleanup_linux_sandbox_async", broken_cleanup)

    assert asyncio.run(router.get_ai_response([], 1)) == LoopResult("ok")
    assert [call["provider"] for call in chat_loop.calls] == ["openai"]
