"""所有 AI 任务共用的回退执行器：顺序、跳过、不换的情况、安全拦截与熔断。"""

import asyncio
from dataclasses import replace
from types import MappingProxyType

import pytest

from fogmoe_telegram_bot.core import ai_providers
from fogmoe_telegram_bot.core.ai_providers import Capabilities
from fogmoe_telegram_bot.features.ai.context_budget import ContextBudgetExceededError
from fogmoe_telegram_bot.features.ai.errors import SafetyBlockError
from fogmoe_telegram_bot.features.ai.task_runner import (
    AllProvidersFailedError,
    ProviderCircuit,
    run_with_fallback,
)
from fogmoe_telegram_bot.features.ai.types import PartialAIResponseError, TurnDeadlineError

SAFETY_TEXT = "Response was blocked: SAFETY"


def make_circuit():
    return ProviderCircuit(failure_threshold=3, window_seconds=300, cooldown_seconds=1800)


def scripted(outcomes):
    """按 (provider, model) 返回结果或抛错的尝试替身；没写到的组合返回 `provider:model`。"""
    calls = []

    async def attempt(candidate):
        calls.append((candidate.provider, candidate.model))
        outcome = outcomes.get((candidate.provider, candidate.model))
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome or f"{candidate.provider}:{candidate.model}"

    return attempt, calls


@pytest.fixture
def summary_order(settings_override):
    settings_override(
        AI_SUMMARY_PROVIDER="gemini",
        AI_SUMMARY_FALLBACK_PROVIDER="openai",
        GEMINI_SUMMARY_MODEL="g1",
        GEMINI_SUMMARY_FALLBACK_MODEL="g2",
        OPENAI_SUMMARY_MODEL="o1",
    )


# -- 熔断 ------------------------------------------------------------------------------


def test_circuit_opens_after_three_consecutive_failures_in_window():
    circuit = make_circuit()
    circuit.record_failure("gemini", now=100.0)
    circuit.record_failure("gemini", now=200.0)

    assert circuit.is_open("gemini", now=250.0) is False

    circuit.record_failure("gemini", now=300.0)

    assert circuit.is_open("gemini", now=300.0) is True
    assert circuit.is_open("gemini", now=300.0 + 1800 - 1) is True
    assert circuit.is_open("gemini", now=300.0 + 1800) is False


def test_circuit_does_not_count_failures_outside_window():
    circuit = make_circuit()
    circuit.record_failure("gemini", now=0.0)
    circuit.record_failure("gemini", now=301.0)
    circuit.record_failure("gemini", now=302.0)

    assert circuit.is_open("gemini", now=302.0) is False
    assert circuit.failure_count("gemini") == 2


def test_circuit_success_resets_the_consecutive_failure_streak():
    circuit = make_circuit()
    circuit.record_failure("gemini", now=100.0)
    circuit.record_failure("gemini", now=200.0)
    circuit.record_success("gemini")
    circuit.record_failure("gemini", now=300.0)

    assert circuit.is_open("gemini", now=300.0) is False
    assert circuit.failure_count("gemini") == 1


# -- 顺序与跳过 ---------------------------------------------------------------------------


def test_providers_and_their_models_are_tried_in_order(summary_order):
    attempt, calls = scripted(
        {("gemini", "g1"): RuntimeError("g1 down"), ("gemini", "g2"): RuntimeError("g2 down")}
    )

    assert asyncio.run(run_with_fallback("summary", attempt)) == "openai:o1"
    assert calls == [("gemini", "g1"), ("gemini", "g2"), ("openai", "o1")]


def test_unknown_unconfigured_and_incapable_providers_are_skipped(settings_override, monkeypatch):
    settings_override(
        AI_CHAT_ORDER="typo-provider,openai,azure,gemini",
        OPENAI_CHAT_MODEL="gpt",
        GEMINI_CHAT_MODEL="gemini-chat",
    )
    patched = {
        key: replace(spec, capabilities=Capabilities(tools=False, vision=True))
        if spec.name == "openai"
        else spec
        for key, spec in ai_providers._BY_NAME.items()
    }
    monkeypatch.setattr(ai_providers, "_BY_NAME", MappingProxyType(patched))
    attempt, calls = scripted({})

    # openai 没有工具能力，azure 没有配置模型，typo-provider 不存在。
    assert asyncio.run(run_with_fallback("chat", attempt)) == "gemini:gemini-chat"
    assert calls == [("gemini", "gemini-chat")]


def test_providers_with_an_open_circuit_are_skipped(summary_order):
    circuit = make_circuit()
    for _ in range(3):
        circuit.record_failure("gemini")
    attempt, calls = scripted({})

    assert asyncio.run(run_with_fallback("summary", attempt, circuit=circuit)) == "openai:o1"
    assert calls == [("openai", "o1")]


def test_all_failures_raise_with_the_last_error(summary_order):
    last = RuntimeError("openai down")
    attempt, calls = scripted(
        {
            ("gemini", "g1"): RuntimeError("g1 down"),
            ("gemini", "g2"): RuntimeError("g2 down"),
            ("openai", "o1"): last,
        }
    )

    with pytest.raises(AllProvidersFailedError, match="All providers failed for AI task: summary") as exc_info:
        asyncio.run(run_with_fallback("summary", attempt))

    assert exc_info.value.last_error is last
    assert len(calls) == 3


def test_an_empty_order_fails_without_an_attempt(settings_override):
    settings_override(AI_CHAT_ORDER="")
    attempt, calls = scripted({})

    with pytest.raises(AllProvidersFailedError) as exc_info:
        asyncio.run(run_with_fallback("chat", attempt))

    assert exc_info.value.last_error is None
    assert calls == []


# -- 不换模型的失败 --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        ContextBudgetExceededError(150_001, 150_000),
        PartialAIResponseError("after tools", [{"type": "tool_result"}]),
        TurnDeadlineError("deadline", "model", []),
    ],
)
def test_context_partial_and_deadline_errors_stop_the_fallback(summary_order, error):
    attempt, calls = scripted({("gemini", "g1"): error})

    with pytest.raises(type(error)):
        asyncio.run(run_with_fallback("summary", attempt))

    assert calls == [("gemini", "g1")]


def test_a_context_error_in_the_cause_chain_also_stops_the_fallback(summary_order):
    try:
        raise ContextBudgetExceededError(150_001, 150_000)
    except ContextBudgetExceededError as cause:
        wrapped = RuntimeError("provider rejected the request")
        wrapped.__cause__ = cause
    attempt, calls = scripted({("gemini", "g1"): wrapped})

    with pytest.raises(RuntimeError, match="provider rejected"):
        asyncio.run(run_with_fallback("summary", attempt))

    assert calls == [("gemini", "g1")]


def test_read_only_agents_rerun_after_tools_but_not_past_a_deadline_or_context_limit(summary_order):
    attempt, calls = scripted(
        {("gemini", "g1"): PartialAIResponseError("after read-only tools", [{"type": "tool_result"}])}
    )

    assert asyncio.run(run_with_fallback("summary", attempt, tools_are_read_only=True)) == "gemini:g2"
    assert calls == [("gemini", "g1"), ("gemini", "g2")]

    try:
        raise ContextBudgetExceededError(150_001, 150_000)
    except ContextBudgetExceededError as cause:
        oversized = PartialAIResponseError("oversized", [{"type": "tool_result"}])
        oversized.__cause__ = cause
    for error in (oversized, TurnDeadlineError("deadline", "model", [])):
        attempt, calls = scripted({("gemini", "g1"): error})
        with pytest.raises(type(error)):
            asyncio.run(run_with_fallback("summary", attempt, tools_are_read_only=True))
        assert calls == [("gemini", "g1")]


# -- 安全拦截与熔断 ------------------------------------------------------------------------


def test_gemini_safety_text_tries_its_fallback_model_then_the_next_provider(summary_order):
    circuit = make_circuit()
    attempt, calls = scripted(
        {("gemini", "g1"): RuntimeError(SAFETY_TEXT), ("gemini", "g2"): RuntimeError(SAFETY_TEXT)}
    )

    assert asyncio.run(run_with_fallback("summary", attempt, circuit=circuit)) == "openai:o1"
    assert calls == [("gemini", "g1"), ("gemini", "g2"), ("openai", "o1")]
    assert circuit.failure_count("gemini") == 0


def test_a_safety_block_on_the_last_model_of_a_provider_that_does_not_fall_through_is_raised(
    settings_override,
):
    settings_override(AI_SUMMARY_PROVIDER="openai", AI_SUMMARY_FALLBACK_PROVIDER="gemini", OPENAI_SUMMARY_MODEL="o1")
    attempt, calls = scripted({("openai", "o1"): SafetyBlockError("blocked")})

    with pytest.raises(SafetyBlockError):
        asyncio.run(run_with_fallback("summary", attempt))

    assert calls == [("openai", "o1")]


def test_safety_text_is_an_ordinary_failure_for_providers_that_do_not_declare_it(settings_override):
    settings_override(AI_SUMMARY_PROVIDER="openai", OPENAI_SUMMARY_MODEL="o1")
    attempt, _ = scripted({("openai", "o1"): RuntimeError(SAFETY_TEXT)})

    with pytest.raises(AllProvidersFailedError) as exc_info:
        asyncio.run(run_with_fallback("summary", attempt))

    assert not isinstance(exc_info.value.last_error, SafetyBlockError)


def test_the_circuit_counts_one_failure_per_failed_provider_and_resets_on_success(summary_order):
    circuit = make_circuit()
    circuit.record_failure("openai")
    attempt, _ = scripted(
        {("gemini", "g1"): RuntimeError("g1 down"), ("gemini", "g2"): RuntimeError("g2 down")}
    )

    assert asyncio.run(run_with_fallback("summary", attempt, circuit=circuit)) == "openai:o1"
    assert circuit.failure_count("gemini") == 1
    assert circuit.failure_count("openai") == 0
