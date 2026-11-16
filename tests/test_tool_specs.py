"""单点 `TOOL_SPECS` 与 `AgentSpec` 声明、动态工具可见性过滤、媒体协议分派、共用限流器与 §6 熔断边界测试。"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import replace
from types import SimpleNamespace

from fogmoe_telegram_bot.core import ai_providers, config
from fogmoe_telegram_bot.features.ai import (
    outbound,
    router,
    task_runner,
    telegram_visible_sender,
    tool_loop_deadline,
    tool_loop_delivery,
    tool_loop_execution,
)
from fogmoe_telegram_bot.features.ai.agents import MAIN_AGENT
from fogmoe_telegram_bot.features.ai.recap import RECAP_AGENT
from fogmoe_telegram_bot.features.ai.summary import SUMMARY_AGENT
from fogmoe_telegram_bot.features.ai.tools import (
    TOOL_SPEC_BY_NAME,
    TOOL_SPECS,
    ToolExecution,
    agent_tool_specs,
    build_tool_context,
    get_media_tool_spec,
    resolve_agent_tool_specs,
)
from fogmoe_telegram_bot.features.ai.tools.rate_limit import (
    NonBlockingConcurrencyLimiter,
    SlidingWindowRateLimiter,
    UserSessionGate,
    reset_all,
)

AGENTS = (MAIN_AGENT, RECAP_AGENT, SUMMARY_AGENT)


def _names(specs) -> list[str]:
    return [spec.name for spec in specs]


def test_tool_specs_is_single_source_of_truth_for_all_20_tools():
    assert len(TOOL_SPECS) == 20
    assert len(TOOL_SPEC_BY_NAME) == 20

    main_names = [spec.name for spec in TOOL_SPECS if "main" in spec.agents]
    recap_names = [spec.name for spec in TOOL_SPECS if "recap" in spec.agents]
    summary_names = [spec.name for spec in TOOL_SPECS if "summary" in spec.agents]

    assert _names(agent_tool_specs(MAIN_AGENT.name)) == main_names
    assert _names(agent_tool_specs(RECAP_AGENT.name)) == recap_names
    assert _names(agent_tool_specs(SUMMARY_AGENT.name)) == summary_names


def test_every_tool_belongs_only_to_declared_agents():
    declared = {agent.name for agent in AGENTS}
    for spec in TOOL_SPECS:
        assert spec.agents, spec.name
        assert set(spec.agents) <= declared, spec.name


def test_declared_execution_matches_the_handler():
    for spec in TOOL_SPECS:
        is_async = inspect.iscoroutinefunction(spec.handler)
        assert (spec.execution is ToolExecution.ASYNC) == is_async, spec.name


def test_required_context_fields_are_ones_an_entry_provides():
    entry_fields = set(build_tool_context(user_id=1, user_state_prompt=""))
    summary_fields = {"user_id", "summary_record_id"}
    for spec in TOOL_SPECS:
        provided = entry_fields | (summary_fields if "summary" in spec.agents else set())
        assert set(spec.requires_context) <= provided, spec.name


def test_declared_limits_are_the_values_the_tools_enforce(settings_override):
    from fogmoe_telegram_bot.features.ai.tools import (
        image_tools,
        sandbox_tools,
        voice_tools,
    )

    image = TOOL_SPEC_BY_NAME["generate_image"].limits()
    assert image.per_user.max_calls == image_tools.IMAGE_RATE_LIMIT_MAX_GENERATIONS
    assert image.per_user.window_seconds == image_tools.IMAGE_RATE_LIMIT_WINDOW_SECONDS
    voice = TOOL_SPEC_BY_NAME["generate_voice"].limits()
    assert voice.per_user.max_calls == voice_tools.VOICE_RATE_LIMIT_MAX_GENERATIONS
    sandbox = TOOL_SPEC_BY_NAME["linux_sandbox"].limits()
    assert sandbox.per_request == sandbox_tools.MAX_CALLS_PER_REQUEST
    assert sandbox.cooldown_seconds == sandbox_tools.USER_SANDBOX_CREATE_COOLDOWN_SECONDS
    assert sandbox.max_concurrent == sandbox_tools.MAX_CONCURRENT_SANDBOXES

    # advisor 的限额在配置里，声明在调用时读，跟着配置变。
    settings_override(AI_ADVISOR_MAX_CALLS_PER_REQUEST=2, AI_ADVISOR_RATE_LIMIT_MAX_CALLS=7)
    advisor = TOOL_SPEC_BY_NAME["advisor"].limits()
    assert advisor.per_request == 2
    assert advisor.per_user.max_calls == 7


def test_only_summary_and_recap_may_switch_models_after_their_tools_ran():
    assert MAIN_AGENT.tools_are_read_only is False
    assert RECAP_AGENT.tools_are_read_only is True
    assert SUMMARY_AGENT.tools_are_read_only is True


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


def test_resolve_agent_tool_specs_hides_unconfigured_external_tools():
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
    visible_names = _names(resolve_agent_tool_specs("main", settings=minimal))

    unconfigured = {
        "google_search",
        "advisor",
        "execute_python_code",
        "linux_sandbox",
        "generate_image",
        "generate_voice",
    }
    # 只去掉没配置凭据的工具，其余工具保持声明顺序；不按入口或聊天类型裁剪。
    assert visible_names == [
        name for name in _names(agent_tool_specs("main")) if name not in unconfigured
    ]
    assert "execute_telegram_command" in visible_names
    assert "fetch_group_context" in visible_names

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
    assert resolve_agent_tool_specs("main", settings=configured) == agent_tool_specs("main")


def test_generate_voice_visible_when_model_and_reference_fall_back_to_defaults():
    # generate_voice_tool 对留空的模型与参考音色使用默认值，可见性判断必须与之一致
    blank_voice_settings = config.AppSettings.from_values(
        FISH_AUDIO_API_KEY="fish-key",
        FISH_AUDIO_MODEL="",
        FISH_AUDIO_REFERENCE_ID="  ",
    )
    visible_names = set(
        _names(resolve_agent_tool_specs("main", settings=blank_voice_settings))
    )

    assert "generate_voice" in visible_names


def test_resolve_agent_tool_specs_drops_provider_skip_tools():
    settings = config.AppSettings.from_values()
    provider_with_skip = replace(
        ai_providers.require("zai"),
        skip_tools=("fetch_url",),
    )

    names = _names(resolve_agent_tool_specs("main", settings=settings))
    skipped_names = _names(
        resolve_agent_tool_specs("main", provider=provider_with_skip, settings=settings)
    )

    assert "fetch_url" in names
    assert skipped_names == [name for name in names if name != "fetch_url"]


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


def test_tools_return_an_error_without_their_declared_context_fields():
    """声明的上下文字段缺了，处理函数自己返回错误（不在循环里拦）；代执行命令的同类检查在它自己的测试里。"""
    from fogmoe_telegram_bot.features.ai.tools.context import tool_request_scope

    full_context = {
        "user_id": 1,
        "is_group": True,
        "group_id": -100,
        "chat_id": -100,
        "message_id": 5,
        "summary_record_id": 9,
    }
    arguments = {"fetch_group_context": {}, "search_prior_context": {"query": "x"}}
    for name, args in arguments.items():
        spec = TOOL_SPEC_BY_NAME[name]
        assert spec.requires_context, name
        for field in spec.requires_context:
            context = {key: value for key, value in full_context.items() if key != field}
            with tool_request_scope(context):
                result = asyncio.run(spec.handler(**args))
            assert "error" in result, (name, field)
