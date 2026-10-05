"""主聊天的统一 provider 入口：模型来自声明、provider 特有行为来自声明。"""

import asyncio
from types import SimpleNamespace

import pytest

from fogmoe_telegram_bot.features.ai import chat_provider
from fogmoe_telegram_bot.features.ai.context_budget import ContextBudgetExceededError
from fogmoe_telegram_bot.features.ai.errors import SafetyBlockError
from fogmoe_telegram_bot.core.deadline import Deadline
from fogmoe_telegram_bot.features.ai.types import PartialAIResponseError, TurnDeadlineError


@pytest.fixture
def tool_loop(monkeypatch):
    """替换工具循环：记录每次调用，按 `behaviour` 返回或抛错。"""
    calls = []
    behaviour = SimpleNamespace(handler=lambda model: ("ok", []))

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
        return behaviour.handler(model)

    monkeypatch.setattr(chat_provider, "run_tool_loop", fake_run_tool_loop)
    return SimpleNamespace(calls=calls, behaviour=behaviour)


def test_provider_runs_the_tool_loop_with_its_declared_chat_model(tool_loop):
    messages = [{"role": "user", "content": "hello"}]
    context = {"user_id": 123}
    settings = SimpleNamespace(FOGMOE_CHAT_MODEL="gpt-5.6-luna")

    response = asyncio.run(chat_provider.run_chat_provider(
        "fogmoe", messages, 123, context, settings=settings
    ))

    assert response == ("ok", [])
    (call,) = tool_loop.calls
    assert call["provider"] == "fogmoe"
    assert call["model"] == "gpt-5.6-luna"
    assert call["messages"] is messages
    assert call["tool_context"] is context
    assert call["kwargs"]["provider_name"] == "FOGMOE"
    assert "completion_kwargs" not in call["kwargs"]


def test_visible_content_handler_reaches_the_tool_loop(tool_loop):
    handler = object()

    asyncio.run(chat_provider.run_chat_provider(
        "openai",
        [],
        1,
        visible_content_handler=handler,
        settings=SimpleNamespace(OPENAI_CHAT_MODEL="gpt"),
    ))

    assert tool_loop.calls[0]["kwargs"]["visible_content_handler"] is handler


def test_the_turn_deadline_reaches_the_tool_loop(tool_loop):
    deadline = Deadline(60)

    asyncio.run(
        chat_provider.run_chat_provider(
            "openai",
            [],
            1,
            settings=SimpleNamespace(OPENAI_CHAT_MODEL="gpt"),
            deadline=deadline,
        )
    )

    assert tool_loop.calls[0]["kwargs"]["deadline"] is deadline


def test_zai_hides_web_tools_and_zhipu_resolves_to_it(tool_loop):
    settings = SimpleNamespace(ZHIPU_CHAT_MODEL="glm-chat")

    asyncio.run(chat_provider.run_chat_provider("zhipu", [], 1, settings=settings))
    asyncio.run(chat_provider.run_chat_provider("zai", [], 1, settings=settings))

    for call in tool_loop.calls:
        assert call["provider"] == "zai"
        assert call["model"] == "glm-chat"
        assert call["kwargs"]["provider_name"] == "Z.ai"
        assert set(call["kwargs"]["skip_tools"]) == {"web_search", "web_browser"}


def test_other_providers_hide_no_tools(tool_loop):
    asyncio.run(chat_provider.run_chat_provider(
        "openai", [], 1, settings=SimpleNamespace(OPENAI_CHAT_MODEL="gpt")
    ))

    assert not tool_loop.calls[0]["kwargs"]["skip_tools"]


def test_missing_chat_model_fails_before_calling_the_model(tool_loop):
    with pytest.raises(RuntimeError, match="Missing OPENAI_CHAT_MODEL configuration"):
        asyncio.run(chat_provider.run_chat_provider("openai", [], 1, settings=SimpleNamespace()))

    assert tool_loop.calls == []


def test_unknown_provider_is_rejected(tool_loop):
    with pytest.raises(RuntimeError, match="Unsupported AI provider"):
        asyncio.run(chat_provider.run_chat_provider("nope", [], 1, settings=SimpleNamespace()))


def test_gemini_retries_with_its_fallback_model(tool_loop):
    def behave(model):
        if model == "primary":
            raise RuntimeError("primary down")
        return f"from {model}", []

    tool_loop.behaviour.handler = behave
    settings = SimpleNamespace(
        GEMINI_CHAT_MODEL="primary", GEMINI_CHAT_FALLBACK_MODEL="fallback"
    )

    response = asyncio.run(chat_provider.run_chat_provider("gemini", [], 1, settings=settings))

    assert response == ("from fallback", [])
    assert [call["model"] for call in tool_loop.calls] == ["primary", "fallback"]


def test_gemini_uses_the_fallback_when_the_primary_model_is_not_configured(tool_loop):
    settings = SimpleNamespace(GEMINI_CHAT_MODEL=None, GEMINI_CHAT_FALLBACK_MODEL="fallback")

    asyncio.run(chat_provider.run_chat_provider("gemini", [], 1, settings=settings))

    assert [call["model"] for call in tool_loop.calls] == ["fallback"]


@pytest.mark.parametrize(
    "error",
    [
        ContextBudgetExceededError(150_001, 150_000),
        PartialAIResponseError("after tools", [{"type": "tool_result"}]),
        TurnDeadlineError("deadline", "model", []),
    ],
)
def test_context_and_partial_errors_skip_the_fallback_model(tool_loop, error):
    def behave(model):
        raise error

    tool_loop.behaviour.handler = behave
    settings = SimpleNamespace(
        GEMINI_CHAT_MODEL="primary", GEMINI_CHAT_FALLBACK_MODEL="fallback"
    )

    with pytest.raises(type(error)):
        asyncio.run(chat_provider.run_chat_provider("gemini", [], 1, settings=settings))

    assert [call["model"] for call in tool_loop.calls] == ["primary"]


SAFETY_TEXT = "Response was blocked: SAFETY"


def test_gemini_safety_block_without_a_fallback_becomes_safety_block_error(tool_loop):
    def behave(model):
        raise RuntimeError(SAFETY_TEXT)

    tool_loop.behaviour.handler = behave
    settings = SimpleNamespace(GEMINI_CHAT_MODEL="primary", GEMINI_CHAT_FALLBACK_MODEL=None)

    with pytest.raises(SafetyBlockError):
        asyncio.run(chat_provider.run_chat_provider("gemini", [], 1, settings=settings))


def test_gemini_fallback_failure_propagates_unchanged(tool_loop):
    def behave(model):
        raise RuntimeError(SAFETY_TEXT if model == "fallback" else "primary down")

    tool_loop.behaviour.handler = behave
    settings = SimpleNamespace(
        GEMINI_CHAT_MODEL="primary", GEMINI_CHAT_FALLBACK_MODEL="fallback"
    )

    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(chat_provider.run_chat_provider("gemini", [], 1, settings=settings))

    assert not isinstance(exc_info.value, SafetyBlockError)


def test_other_providers_do_not_translate_safety_text(tool_loop):
    def behave(model):
        raise RuntimeError(SAFETY_TEXT)

    tool_loop.behaviour.handler = behave

    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(chat_provider.run_chat_provider(
            "openai", [], 1, settings=SimpleNamespace(OPENAI_CHAT_MODEL="gpt")
        ))

    assert not isinstance(exc_info.value, SafetyBlockError)
