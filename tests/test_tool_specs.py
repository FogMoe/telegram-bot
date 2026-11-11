"""单点 `TOOL_SPECS` 声明、动态工具可见性过滤、媒体协议分派、共用限流器与 §6 熔断边界测试。"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import replace
from types import SimpleNamespace

from fogmoe_telegram_bot.core import ai_providers, config
from fogmoe_telegram_bot.features.ai import (
    idle_followup,
    outbound,
    router,
    summary,
    task_runner,
    telegram_visible_sender,
    tool_loop_deadline,
    tool_loop_delivery,
    tool_loop_execution,
)
from fogmoe_telegram_bot.features.ai.tools import (
    AI_TOOL_ARG_MODELS,
    AI_TOOL_HANDLERS,
    OPENAI_TOOLS,
    TOOL_SPEC_BY_NAME,
    TOOL_SPECS,
    build_tool_context,
    get_media_tool_spec,
    resolve_agent_tools,
)
from fogmoe_telegram_bot.features.ai.tools.rate_limit import (
    NonBlockingConcurrencyLimiter,
    SlidingWindowRateLimiter,
    UserSessionGate,
    reset_all,
)
from fogmoe_telegram_bot.features.ai.usage import (
    DEFAULT_BILLED_TOOL_CATEGORIES,
    ai_usage_scope,
)


def _tool_names(tools: list[dict]) -> list[str]:
    return [tool["function"]["name"] for tool in tools]


def test_tool_specs_is_single_source_of_truth_for_all_20_tools():
    assert len(TOOL_SPECS) == 20
    assert len(TOOL_SPEC_BY_NAME) == 20
    assert set(AI_TOOL_ARG_MODELS) == set(TOOL_SPEC_BY_NAME)

    main_names = [spec.name for spec in TOOL_SPECS if "main" in spec.agents]
    recap_names = [spec.name for spec in TOOL_SPECS if "recap" in spec.agents]
    summary_names = [spec.name for spec in TOOL_SPECS if "summary" in spec.agents]

    assert _tool_names(OPENAI_TOOLS) == main_names
    assert set(AI_TOOL_HANDLERS) == set(main_names)
    assert _tool_names(idle_followup.IDLE_RECAP_TOOLS) == recap_names
    assert set(idle_followup.IDLE_RECAP_TOOL_HANDLERS) == set(recap_names)
    assert _tool_names(summary.SUMMARY_TOOLS) == summary_names
    assert set(summary.SUMMARY_TOOL_HANDLERS) == set(summary_names)

    spec_billed = {
        spec.name: spec.billing_category
        for spec in TOOL_SPECS
        if spec.billing_category is not None
    }
    assert spec_billed == dict(DEFAULT_BILLED_TOOL_CATEGORIES)


def test_loop_and_delivery_modules_have_no_hardcoded_media_tool_branches():
    for module in (
        tool_loop_delivery,
        tool_loop_deadline,
        tool_loop_execution,
        telegram_visible_sender,
        outbound,
    ):
        source = inspect.getsource(module)
        assert '"generate_image"' not in source
        assert '"generate_voice"' not in source

    assert get_media_tool_spec("generate_image") is not None
    assert get_media_tool_spec("generate_voice") is not None
    assert get_media_tool_spec("google_search") is None


def test_resolve_agent_tools_hides_unconfigured_external_tools():
    minimal = config.AppSettings.from_values(
        SERPAPI_API_KEY=None,
        JUDGE0_API_URL="",
        E2B_API_KEY=None,
        IMAGE_GEN_API_URL="",
        IMAGE_GEN_API_TOKEN="",
        FISH_AUDIO_API_KEY=None,
        AI_ADVISOR_PROVIDER=None,
        AI_ADVISOR_FALLBACK_PROVIDER=None,
    )
    private_ctx = build_tool_context(
        user_id=1,
        user_state_prompt="s",
        chat_id=1,
        chat_type="private",
        message_id=10,
    )

    visible_names = set(
        _tool_names(
            resolve_agent_tools(
                "main",
                entry="user",
                tool_context=private_ctx,
                settings=minimal,
            )
        )
    )

    for unconfigured in (
        "google_search",
        "advisor",
        "execute_python_code",
        "linux_sandbox",
        "generate_image",
        "generate_voice",
        "fetch_group_context",
    ):
        assert unconfigured not in visible_names
    assert "execute_telegram_command" in visible_names
    assert "user_diary" in visible_names

    configured = config.AppSettings.from_values(
        SERPAPI_API_KEY="serp-key",
        JUDGE0_API_URL="https://ce.judge0.com",
        E2B_API_KEY="e2b-key",
        IMAGE_GEN_API_URL="https://img.example/v1",
        IMAGE_GEN_API_TOKEN="img-token",
        FISH_AUDIO_API_KEY="fish-key",
        AI_ADVISOR_PROVIDER="openai",
        OPENAI_ADVISOR_MODEL="gpt-advisor",
    )
    group_ctx = build_tool_context(
        user_id=1,
        user_state_prompt="s",
        chat_id=-10099,
        chat_type="supergroup",
        message_id=11,
    )
    full_names = set(
        _tool_names(
            resolve_agent_tools(
                "main",
                entry="user",
                tool_context=group_ctx,
                settings=configured,
            )
        )
    )
    assert full_names == set(_tool_names(OPENAI_TOOLS))


def test_generate_voice_visible_when_model_and_reference_fall_back_to_defaults():
    # generate_voice_tool 对留空的模型与参考音色使用默认值，可见性判断必须与之一致
    blank_voice_settings = config.AppSettings.from_values(
        FISH_AUDIO_API_KEY="fish-key",
        FISH_AUDIO_MODEL="",
        FISH_AUDIO_REFERENCE_ID="  ",
    )
    private_ctx = build_tool_context(
        user_id=1,
        user_state_prompt="s",
        chat_id=1,
        chat_type="private",
        message_id=10,
    )

    visible_names = set(
        _tool_names(
            resolve_agent_tools(
                "main",
                entry="user",
                tool_context=private_ctx,
                settings=blank_voice_settings,
            )
        )
    )

    assert "generate_voice" in visible_names


def test_resolve_agent_tools_filters_by_entry_chat_type_and_provider_skip_tools():
    settings = config.AppSettings.from_values()
    bg_ctx = build_tool_context(user_id=1, user_state_prompt="s")

    with ai_usage_scope(entry="schedule", task="chat"):
        schedule_names = set(
            _tool_names(
                resolve_agent_tools("main", tool_context=bg_ctx, settings=settings)
            )
        )
    assert "execute_telegram_command" not in schedule_names
    assert "fetch_group_context" not in schedule_names

    provider_with_skip = replace(
        ai_providers.require("zai"),
        skip_tools=("fetch_url",),
    )
    private_ctx = build_tool_context(
        user_id=1,
        user_state_prompt="s",
        chat_id=1,
        chat_type="private",
        message_id=5,
    )
    skipped_names = set(
        _tool_names(
            resolve_agent_tools(
                "main",
                entry="user",
                provider=provider_with_skip,
                tool_context=private_ctx,
                settings=settings,
            )
        )
    )
    assert "fetch_url" not in skipped_names
    assert "execute_telegram_command" in skipped_names


def test_shared_rate_limiters_enforce_window_rollback_concurrency_and_session_caps():
    now = 100.0
    limiter = SlidingWindowRateLimiter(clock=lambda: now)
    concurrency = NonBlockingConcurrencyLimiter(lambda: 2)
    gate = UserSessionGate(clock=lambda: now)

    try:
        r1 = limiter.reserve("u1", window_seconds=60, max_calls=2)
        r2 = limiter.reserve("u1", window_seconds=60, max_calls=2)
        r3 = limiter.reserve("u1", window_seconds=60, max_calls=2)
        assert r1.allowed and r2.allowed and not r3.allowed
        assert r3.retry_after_seconds == 60

        limiter.release("u1", r2.token)
        assert limiter.reserve("u1", window_seconds=60, max_calls=2).allowed

        assert concurrency.acquire() is True
        assert concurrency.acquire() is True
        assert concurrency.acquire() is False
        concurrency.release()
        assert concurrency.acquire() is True

        s1 = gate.reserve(
            "u1",
            active_ttl_seconds=300,
            cooldown_seconds=120,
            max_concurrent_sessions=1,
        )
        assert s1.allowed
        s_global_busy = gate.reserve(
            "u2",
            active_ttl_seconds=300,
            cooldown_seconds=120,
            max_concurrent_sessions=1,
        )
        assert not s_global_busy.allowed
        assert s_global_busy.blocked_reason == "global_sandbox_busy"

        gate.release_active("u1")
        s_cooldown = gate.reserve(
            "u1",
            active_ttl_seconds=300,
            cooldown_seconds=120,
            max_concurrent_sessions=2,
        )
        assert not s_cooldown.allowed
        assert s_cooldown.blocked_reason == "user_rate_limit"
        assert s_cooldown.retry_after_seconds == 120
    finally:
        reset_all()

    assert limiter.buckets == {}
    assert concurrency.inflight == 0
    assert gate.active_users == {}
    assert gate.cooldowns == {}


def test_non_chat_tasks_do_not_share_interactive_chat_circuit(
    settings_override, monkeypatch
):
    settings_override(
        AI_CHAT_ORDER="openai,gemini",
        OPENAI_CHAT_MODEL="gpt-chat",
        GEMINI_CHAT_MODEL="gemini-chat",
        AI_TRANSLATE_PROVIDER="openai",
        OPENAI_TRANSLATE_MODEL="gpt-translate",
    )
    router.CHAT_CIRCUIT.reset()
    try:
        for _ in range(router.AI_PROVIDER_CIRCUIT_FAILURE_THRESHOLD):
            router.CHAT_CIRCUIT.record_failure("openai")
        assert router.CHAT_CIRCUIT.is_open("openai")

        seen_tasks: list[tuple[str, str]] = []

        async def fake_completion(provider, model, messages, **kwargs):
            seen_tasks.append((provider, model))
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="translated", tool_calls=[]))]
            )

        monkeypatch.setattr(task_runner, "create_chat_completion", fake_completion)

        result = asyncio.run(
            task_runner.run_ai_task(
                "translate",
                messages=[{"role": "user", "content": "hi"}],
            )
        )
        assert result.choices[0].message.content == "translated"
        assert seen_tasks == [("openai", "gpt-translate")]
    finally:
        router.CHAT_CIRCUIT.reset()
