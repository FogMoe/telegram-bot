from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from features.ai.tools import advisor_tools
from features.ai.tools.context import (
    clear_tool_request_context,
    set_tool_request_context,
)
from features.ai.tools.models import AdvisorArgs, parameters_schema
from features.ai.tools.registry import AI_TOOL_HANDLERS
from features.ai.tools.schemas import OPENAI_TOOLS


def _response(content: str = "Use option B.") -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content),
            )
        ],
        model="senior-advisor",
        usage=SimpleNamespace(total_tokens=321),
    )


@pytest.fixture(autouse=True)
def _clear_advisor_state():
    advisor_tools._RATE_LIMITS.clear()
    clear_tool_request_context()
    yield
    advisor_tools._RATE_LIMITS.clear()
    clear_tool_request_context()


def _prepare_advisor(monkeypatch):
    monkeypatch.setattr(advisor_tools, "_configured_advisor_available", lambda: True)
    monkeypatch.setattr(advisor_tools.config, "AI_ADVISOR_MAX_CALLS_PER_REQUEST", 1)
    monkeypatch.setattr(advisor_tools.config, "AI_ADVISOR_RATE_LIMIT_MAX_CALLS", 3)
    monkeypatch.setattr(
        advisor_tools.config,
        "AI_ADVISOR_RATE_LIMIT_WINDOW_SECONDS",
        300,
    )


def test_advisor_schema_is_registered_and_bounded():
    schema = parameters_schema(AdvisorArgs)
    tool_names = [tool["function"]["name"] for tool in OPENAI_TOOLS]

    assert "advisor" in tool_names
    assert AI_TOOL_HANDLERS["advisor"] is advisor_tools.advisor_tool
    assert schema["required"] == ["task"]
    assert schema["properties"]["task"]["maxLength"] == 6000
    assert schema["properties"]["context"]["maxLength"] == 12000


@pytest.mark.parametrize(
    "arguments",
    [
        {"task": ""},
        {"task": "x" * 6001},
        {"task": "review", "context": "x" * 12001},
    ],
)
def test_advisor_argument_validation_rejects_invalid_lengths(arguments):
    with pytest.raises(ValidationError):
        AdvisorArgs.model_validate(arguments)


def test_advisor_calls_model_without_tools_or_chat_history(monkeypatch):
    _prepare_advisor(monkeypatch)
    monkeypatch.setattr(advisor_tools.config, "AI_ADVISOR_TIMEOUT_SECONDS", 42)
    monkeypatch.setattr(advisor_tools.config, "AI_ADVISOR_MAX_OUTPUT_TOKENS", 1234)
    set_tool_request_context({"user_id": 123, "private_value": "do-not-forward"})
    recorded = {}

    def fake_run_ai_task(task, messages, **kwargs):
        recorded.update({"task": task, "messages": messages, "kwargs": kwargs})
        return _response()

    monkeypatch.setattr(advisor_tools, "run_ai_task", fake_run_ai_task)

    result = advisor_tools.advisor_tool(
        "  Compare option A and option B.  ",
        "A is faster; B is safer.",
    )

    assert result == {"status": "ok", "advice": "Use option B."}
    assert recorded["task"] == "advisor"
    assert recorded["kwargs"] == {"max_tokens": 1234, "timeout": 42}
    assert [message["role"] for message in recorded["messages"]] == [
        "system",
        "user",
    ]
    assert recorded["messages"][1]["content"] == (
        "Task:\nCompare option A and option B.\n\n"
        "Context:\nA is faster; B is safer."
    )
    assert "do-not-forward" not in str(recorded["messages"])
    assert "tools" not in recorded["kwargs"]


def test_advisor_enforces_one_call_per_request(monkeypatch):
    _prepare_advisor(monkeypatch)
    monkeypatch.setattr(advisor_tools, "run_ai_task", lambda *args, **kwargs: _response())
    set_tool_request_context({"user_id": 123})

    first = advisor_tools.advisor_tool("Review this plan")
    second = advisor_tools.advisor_tool("Review it again")

    assert first["status"] == "ok"
    assert second == {
        "status": "blocked",
        "error": "Advisor call limit reached for this request.",
        "blocked_reason": "call_limit",
    }


def test_advisor_rate_limits_across_requests_for_same_user(monkeypatch):
    _prepare_advisor(monkeypatch)
    monkeypatch.setattr(advisor_tools.config, "AI_ADVISOR_RATE_LIMIT_MAX_CALLS", 1)
    monkeypatch.setattr(advisor_tools.time, "monotonic", lambda: 1000.0)
    monkeypatch.setattr(advisor_tools, "run_ai_task", lambda *args, **kwargs: _response())

    set_tool_request_context({"user_id": 123})
    first = advisor_tools.advisor_tool("First review")
    set_tool_request_context({"user_id": 123})
    second = advisor_tools.advisor_tool("Second review")

    assert first["status"] == "ok"
    assert second == {
        "status": "blocked",
        "error": "The reasoning advisor rate limit has been reached.",
        "blocked_reason": "user_rate_limit",
        "retry_after_seconds": 300,
    }


def test_advisor_rate_limit_prunes_expired_users(monkeypatch):
    _prepare_advisor(monkeypatch)
    now = 1000.0
    monkeypatch.setattr(advisor_tools.time, "monotonic", lambda: now)
    monkeypatch.setattr(advisor_tools, "run_ai_task", lambda *args, **kwargs: _response())

    set_tool_request_context({"user_id": 123})
    assert advisor_tools.advisor_tool("First review")["status"] == "ok"
    assert "123" in advisor_tools._RATE_LIMITS

    now += advisor_tools.config.AI_ADVISOR_RATE_LIMIT_WINDOW_SECONDS + 1
    set_tool_request_context({"user_id": 456})
    assert advisor_tools.advisor_tool("Second review")["status"] == "ok"

    assert "123" not in advisor_tools._RATE_LIMITS
    assert "456" in advisor_tools._RATE_LIMITS


def test_advisor_configuration_check_skips_invalid_primary(monkeypatch):
    monkeypatch.setattr(
        advisor_tools,
        "get_provider_order_for_task",
        lambda task: ["invalid-provider", "openai"],
    )

    def fake_get_models(provider, task):
        if provider == "invalid-provider":
            raise RuntimeError("Unsupported AI provider: invalid-provider")
        return ["fallback-model"]

    monkeypatch.setattr(advisor_tools, "get_models_for_task", fake_get_models)

    assert advisor_tools._configured_advisor_available() is True


def test_advisor_busy_response_does_not_consume_user_rate_limit(monkeypatch):
    _prepare_advisor(monkeypatch)
    set_tool_request_context({"user_id": 123})

    class _BusySemaphore:
        def acquire(self, blocking=False):
            return False

    monkeypatch.setattr(advisor_tools, "_ADVISOR_SEMAPHORE", _BusySemaphore())

    result = advisor_tools.advisor_tool("Review this")

    assert result == {
        "status": "busy",
        "error": "The reasoning advisor is busy. Continue without it.",
    }
    assert advisor_tools._RATE_LIMITS == {}


def test_advisor_returns_sanitized_error(monkeypatch):
    _prepare_advisor(monkeypatch)
    set_tool_request_context({"user_id": 123})

    def fail(*args, **kwargs):
        raise RuntimeError("secret endpoint and credential details")

    monkeypatch.setattr(advisor_tools, "run_ai_task", fail)

    result = advisor_tools.advisor_tool("Review this")

    assert result == {
        "status": "error",
        "error": "The reasoning advisor is temporarily unavailable. Continue without it.",
    }
    assert "secret" not in str(result)
