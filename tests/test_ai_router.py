import asyncio

import pytest

from features.ai import router
from features.ai.context_budget import ContextBudgetExceededError
from features.ai.providers import gemini
from features.ai.types import PartialAIResponseError


@pytest.fixture(autouse=True)
def clear_provider_circuit_state():
    router._provider_failure_streaks.clear()
    router._provider_circuit_open_until.clear()
    yield
    router._provider_failure_streaks.clear()
    router._provider_circuit_open_until.clear()


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
    ):
        calls.append(messages)
        if len(calls) == 1:
            return None, RuntimeError("provider failed")
        return ("text fallback response", []), None

    monkeypatch.setattr(router, "_try_ai_services", fake_try_ai_services)

    response = asyncio.run(router.get_ai_response(image_messages, user_id=123))

    assert response == ("text fallback response", [])
    assert calls == [
        image_messages,
        [{"role": "user", "content": "describe this image"}],
    ]


def test_visible_content_was_sent_counts_media_messages():
    class _VisibleHandler:
        sent_count = 0
        sent_contents = []
        sent_messages = [object()]

    assert router._visible_content_was_sent(_VisibleHandler()) is True


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
        return "ok", []

    monkeypatch.setattr(router, "AI_SERVICE_ORDER", ["siliconflow"])
    monkeypatch.setattr(
        router,
        "AI_SERVICE_MAP",
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

    assert response == ("ok", [])
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
        return "ok", []

    monkeypatch.setattr(router, "AI_SERVICE_ORDER", ["openai"])
    monkeypatch.setattr(router, "AI_SERVICE_MAP", {"openai": fake_service})
    monkeypatch.setattr(router, "chat_service_supports_vision", lambda service_name: True)

    response = asyncio.run(router.get_ai_response(image_messages, user_id=123))

    assert response == ("ok", [])
    assert calls == [image_messages]


def test_provider_circuit_opens_after_three_consecutive_failures_in_window():
    router._record_provider_failure("gemini", now=100.0)
    router._record_provider_failure("gemini", now=200.0)

    assert router._provider_circuit_is_open("gemini", now=250.0) is False

    router._record_provider_failure("gemini", now=300.0)

    assert router._provider_circuit_is_open("gemini", now=300.0) is True
    assert router._provider_circuit_is_open(
        "gemini",
        now=300.0 + router.AI_PROVIDER_CIRCUIT_COOLDOWN_SECONDS - 1,
    ) is True
    assert router._provider_circuit_is_open(
        "gemini",
        now=300.0 + router.AI_PROVIDER_CIRCUIT_COOLDOWN_SECONDS,
    ) is False


def test_provider_circuit_does_not_count_failures_outside_window():
    router._record_provider_failure("gemini", now=0.0)
    router._record_provider_failure(
        "gemini",
        now=router.AI_PROVIDER_CIRCUIT_WINDOW_SECONDS + 1,
    )
    router._record_provider_failure(
        "gemini",
        now=router.AI_PROVIDER_CIRCUIT_WINDOW_SECONDS + 2,
    )

    assert router._provider_circuit_is_open(
        "gemini",
        now=router.AI_PROVIDER_CIRCUIT_WINDOW_SECONDS + 2,
    ) is False


def test_provider_success_resets_consecutive_failure_streak():
    router._record_provider_failure("gemini", now=100.0)
    router._record_provider_failure("gemini", now=200.0)
    router._record_provider_success("gemini")
    router._record_provider_failure("gemini", now=300.0)

    assert router._provider_circuit_is_open("gemini", now=300.0) is False
    assert router._provider_failure_streaks["gemini"] == [300.0]


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
        return "ok", []

    monkeypatch.setattr(router, "AI_SERVICE_ORDER", ["gemini", "siliconflow"])
    monkeypatch.setattr(
        router,
        "AI_SERVICE_MAP",
        {
            "gemini": failing_service,
            "siliconflow": fallback_service,
        },
    )
    monkeypatch.setattr(router, "_provider_circuit_is_open", lambda service_name: service_name == "gemini")

    response = asyncio.run(router.get_ai_response([], user_id=123))

    assert response == ("ok", [])
    assert calls == ["siliconflow"]


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
                [{"type": "tool_result", "tool_name": "advisor"}],
            ) from exc

    monkeypatch.setattr(router, "AI_SERVICE_ORDER", ["gemini"])
    monkeypatch.setattr(router, "AI_SERVICE_MAP", {"gemini": timed_out_service})

    with caplog.at_level("WARNING"):
        response = asyncio.run(router.get_ai_response([], user_id=123))

    timeout_records = [
        record
        for record in caplog.records
        if "timed out after partial AI response" in record.getMessage()
    ]
    assert response[0] == router.PARTIAL_AI_RESPONSE_ERROR_MESSAGE
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
        return "unexpected", []

    monkeypatch.setattr(router, "AI_SERVICE_ORDER", ["openai", "gemini"])
    monkeypatch.setattr(
        router,
        "AI_SERVICE_MAP",
        {"openai": oversized_service, "gemini": fallback_service},
    )

    response = asyncio.run(router.get_ai_response([], user_id=123))

    assert calls == ["openai"]
    assert response[0] == router.CONTEXT_BUDGET_ERROR_MESSAGE
    assert "当前对话内容太多" in response[0]
    assert "精简" not in response[0]
    assert "Token" not in response[0]
    assert "150,001" not in response[0]
    assert response[1] == []
    assert router.runtime_error_cause(response[0]) == "context_budget_exceeded"
    assert router._provider_failure_streaks == {}


def test_context_budget_error_after_tool_result_preserves_tool_logs(monkeypatch):
    tool_logs = [{"type": "tool_result", "tool_name": "lookup", "result": "ok"}]

    def oversized_service(
        messages,
        user_id,
        tool_context=None,
        visible_content_handler=None,
    ):
        try:
            raise ContextBudgetExceededError(150_001, 150_000)
        except ContextBudgetExceededError as exc:
            raise PartialAIResponseError(str(exc), tool_logs) from exc

    monkeypatch.setattr(router, "AI_SERVICE_ORDER", ["openai"])
    monkeypatch.setattr(router, "AI_SERVICE_MAP", {"openai": oversized_service})

    response = asyncio.run(router.get_ai_response([], user_id=123))

    assert router.runtime_error_cause(response[0]) == "context_budget_exceeded"
    assert response[1] == tool_logs


def test_gemini_context_budget_error_skips_model_fallback(monkeypatch):
    calls = []
    monkeypatch.setattr(gemini.config, "GEMINI_CHAT_MODEL", "primary-model")
    monkeypatch.setattr(
        gemini.config,
        "GEMINI_CHAT_FALLBACK_MODEL",
        "fallback-model",
    )

    def fake_run_tool_loop(provider, model, messages, tool_context, **kwargs):
        calls.append(model)
        raise ContextBudgetExceededError(150_001, 150_000)

    monkeypatch.setattr(gemini, "run_tool_loop", fake_run_tool_loop)

    with pytest.raises(ContextBudgetExceededError):
        gemini.get_ai_response([], user_id=123)

    assert calls == ["primary-model"]


def test_runtime_error_cause_only_classifies_fixed_runtime_messages():
    assert (
        router.runtime_error_cause(router.PARTIAL_AI_RESPONSE_ERROR_MESSAGE)
        == "partial_ai_response_failed"
    )
    assert (
        router.runtime_error_cause(router.AI_SERVICE_ERROR_MESSAGE)
        == "all_ai_services_failed"
    )
    assert router.runtime_error_cause("这是 AI 的普通回复") is None
