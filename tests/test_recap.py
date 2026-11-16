"""空闲跟进之前的回顾：回顾 agent 的声明、回复解析与生成。"""

import asyncio

import pytest

from fogmoe_telegram_bot.features.ai import recap, tool_runner
from fogmoe_telegram_bot.features.ai.tools import agent_tool_specs, get_tool_request_context
from fogmoe_telegram_bot.features.ai.types import LoopResult


def test_parse_recap_response_requires_exact_schema():
    result = recap.parse_recap_response(
        '{"recap":"聊了计划", "open_loops":"确认时间", '
        '"suggested_follow_up":"问进展", "memory_suggestion":'
        '{"impression":"喜欢简洁回答", "diary":"正在准备演示"}}'
    )
    assert result == {
        "recap": "聊了计划",
        "open_loops": "确认时间",
        "suggested_follow_up": "问进展",
        "memory_suggestion": {
            "impression": "喜欢简洁回答",
            "diary": "正在准备演示",
        },
    }

    with pytest.raises(ValueError, match="required JSON schema"):
        recap.parse_recap_response(
            '{"recap": [], "open_loops": "", "suggested_follow_up": "", '
            '"memory_suggestion":{"impression":"", "diary":""}}'
        )

    with pytest.raises(ValueError, match="required JSON schema"):
        recap.parse_recap_response(
            '```json\n{"recap":"x","open_loops":"","suggested_follow_up":"",'
            '"memory_suggestion":{"impression":"","diary":""}}\n```'
        )

    with pytest.raises(ValueError, match="was empty"):
        recap.parse_recap_response("")


def test_generate_recap_requests_strict_sdk_json_schema(monkeypatch):
    captured = {}

    async def fake_run_recap_agent(messages, user_id):
        captured.update(messages=messages, user_id=user_id)
        return (
            '{"recap":"聊了计划","open_loops":"",'
            '"suggested_follow_up":"稍后问候","memory_suggestion":'
            '{"impression":"","diary":"准备周五演示"}}'
        )

    monkeypatch.setattr(recap, "run_recap_agent", fake_run_recap_agent)

    result = asyncio.run(recap.generate_recap(
        321,
        [{"role": "user", "content": "最近很忙"}],
        {
            "impression": "喜欢简洁回答",
            "diary_index": [{"page": 1, "title": "Projects", "summary": "旧项目"}],
        },
    ))

    response_format = recap.RECAP_AGENT.completion_kwargs["response_format"]
    assert result["recap"] == "聊了计划"
    assert result["memory_suggestion"]["diary"] == "准备周五演示"
    assert captured["user_id"] == 321
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    schema = response_format["json_schema"]["schema"]
    assert schema["additionalProperties"] is False
    memory_schema = schema["$defs"]["IdleRecapMemorySuggestion"]
    assert memory_schema["additionalProperties"] is False
    assert memory_schema["properties"]["impression"]["maxLength"] == 2000
    assert "喜欢简洁回答" in captured["messages"][0]["content"]
    assert "旧项目" in captured["messages"][0]["content"]


def test_run_recap_agent_exposes_only_read_only_memory_tools(monkeypatch, settings_override):
    captured = {}
    settings_override(AI_RECAP_PROVIDER="gemini", GEMINI_RECAP_MODEL="recap-model")

    async def fake_run_tool_loop(provider, model, messages, tool_context, **kwargs):
        captured.update(
            provider=provider,
            model=model,
            messages=messages,
            tool_context=tool_context,
            kwargs=kwargs,
            active_context=get_tool_request_context(),
        )
        return LoopResult("structured result")

    monkeypatch.setattr(tool_runner, "run_tool_loop", fake_run_tool_loop)

    result = asyncio.run(recap.run_recap_agent(
        [{"role": "user", "content": "review"}],
        456,
    ))

    agent = captured["kwargs"]["agent"]
    tool_names = {spec.name for spec in agent.tools()}
    assert result == "structured result"
    assert captured["provider"] == "gemini"
    assert captured["model"] == "recap-model"
    assert captured["active_context"] == {"user_id": 456}
    assert tool_names == {
        "fetch_permanent_summaries",
        "search_permanent_records",
        "read_diary_page",
    }
    assert agent is recap.RECAP_AGENT
    assert "read_diary_page" not in {spec.name for spec in agent_tool_specs("main")}
    assert "max_tokens" not in agent.completion_kwargs
    assert agent.completion_timeout == 120
    assert agent.completion_kwargs["drop_params"] is False
    assert agent.completion_kwargs["response_format"]["type"] == "json_schema"
    assert get_tool_request_context() == {}
