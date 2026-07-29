import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace

import pytest

from features.ai import idle_followup
from features.ai.tools import get_tool_request_context


def test_claim_due_followups_skips_registered_users_without_coins(monkeypatch):
    captured_queries = []

    @asynccontextmanager
    async def fake_transaction():
        yield SimpleNamespace()

    async def fake_fetch_all(sql, params, **kwargs):
        captured_queries.append((sql, params))
        return []

    monkeypatch.setattr(idle_followup.mysql_connection, "transaction", fake_transaction)
    monkeypatch.setattr(idle_followup.mysql_connection, "fetch_all", fake_fetch_all)

    assert asyncio.run(idle_followup._claim_due_followups()) == []

    query, params = captured_queries[0]
    query = " ".join(query.split())
    assert "LEFT JOIN user AS u ON u.id = f.user_id" in query
    assert "COALESCE(u.coins, 0) + COALESCE(u.coins_paid, 0) > 0" in query
    assert params[-1] == idle_followup.IDLE_FOLLOWUP_BATCH_SIZE


def test_calculate_ttl_uses_default_median_and_hard_bounds():
    assert idle_followup.calculate_ttl_seconds([]) == 10 * 60
    assert idle_followup.calculate_ttl_seconds([10, 30, 60]) == 2 * 60
    assert idle_followup.calculate_ttl_seconds([180, 480, 1200]) == 8 * 60
    assert idle_followup.calculate_ttl_seconds([7200, 10800]) == 60 * 60


def test_extract_recent_dialogue_keeps_real_messages_and_media_only():
    messages = [
        {
            "role": "user",
            "content": (
                '<metadata type="private" timestamp="2026-07-29 10:00:00" user="@kc">\n'
                "</metadata>\n<message>你好 &amp; hello</message>"
            ),
        },
        {"role": "assistant", "content": "你好"},
        {
            "role": "user",
            "content": (
                '<metadata type="private" event="command" command="help">\n'
                "</metadata>\n<message>/help</message>"
            ),
        },
        {
            "role": "user",
            "content": (
                '<metadata type="idle_followup" origin="idle_recap">\n'
                "  <recap>旧回顾</recap>\n</metadata>"
            ),
        },
        {
            "role": "user",
            "content": (
                '<metadata type="private"><media type="photo">'
                "<description>一只猫</description></media></metadata>"
                "<message>看看这个</message>"
            ),
        },
        {"role": "tool", "content": '{"result":"internal"}'},
    ]

    assert idle_followup._extract_recent_dialogue(messages) == [
        {"role": "user", "content": "你好 & hello"},
        {"role": "assistant", "content": "你好"},
        {"role": "user", "content": "看看这个\n[媒体描述] 一只猫"},
    ]


def test_parse_recap_response_requires_exact_schema():
    result = idle_followup._parse_recap_response(
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
        idle_followup._parse_recap_response(
            '{"recap": [], "open_loops": "", "suggested_follow_up": "", '
            '"memory_suggestion":{"impression":"", "diary":""}}'
        )

    with pytest.raises(ValueError, match="required JSON schema"):
        idle_followup._parse_recap_response(
            '```json\n{"recap":"x","open_loops":"","suggested_follow_up":"",'
            '"memory_suggestion":{"impression":"","diary":""}}\n```'
        )


def test_format_idle_recap_event_escapes_output_without_internal_version():
    result = idle_followup._format_idle_recap_event(
        {
            "recap": "用户提到 <计划>",
            "open_loops": "确认 & 回复",
            "suggested_follow_up": "问一下进展",
            "memory_suggestion": {
                "impression": "长期偏好 <简洁>",
                "diary": "正在准备 A & B",
            },
        },
        timestamp=datetime(2026, 7, 29, 12, 0, 0),
    )

    assert 'origin="idle_recap"' in result
    assert "chat_type" not in result
    assert "activity_version" not in result
    assert "<recap>用户提到 &lt;计划&gt;</recap>" in result
    assert "<open_loops>确认 &amp; 回复</open_loops>" in result
    assert "<memory_suggestion>" in result
    assert "<impression>长期偏好 &lt;简洁&gt;</impression>" in result
    assert "<diary>正在准备 A &amp; B</diary>" in result
    assert "<message>" not in result
    assert "<instruction>" not in result


def test_format_idle_recap_event_omits_empty_memory_suggestion():
    result = idle_followup._format_idle_recap_event(
        {
            "recap": "简短回顾",
            "open_loops": "",
            "suggested_follow_up": "",
            "memory_suggestion": {"impression": "", "diary": ""},
        },
        timestamp=datetime(2026, 7, 29, 12, 0, 0),
    )

    assert "<memory_suggestion>" not in result


def test_load_recap_memory_context_uses_saved_impression_and_diary_index(monkeypatch):
    async def fake_impression(_user_id):
        return " 喜欢简洁回答\n"

    async def fake_fetch_all(_sql, _params):
        return [
            (1, "Projects", " Current work "),
            (2, "Relationships", "Important people"),
        ]

    monkeypatch.setattr(
        idle_followup.process_user,
        "async_get_user_impression",
        fake_impression,
    )
    monkeypatch.setattr(idle_followup.mysql_connection, "fetch_all", fake_fetch_all)

    result = asyncio.run(idle_followup._load_recap_memory_context(123))

    assert result == {
        "impression": "喜欢简洁回答",
        "diary_index": [
            {"page": 1, "title": "Projects", "summary": "Current work"},
            {
                "page": 2,
                "title": "Relationships",
                "summary": "Important people",
            },
        ],
    }


def test_generate_recap_requests_strict_sdk_json_schema(monkeypatch):
    captured = {}

    def fake_run_recap_agent(messages, user_id, response_format):
        captured.update(
            messages=messages,
            user_id=user_id,
            response_format=response_format,
        )
        return (
            '{"recap":"聊了计划","open_loops":"",'
            '"suggested_follow_up":"稍后问候","memory_suggestion":'
            '{"impression":"","diary":"准备周五演示"}}'
        )

    monkeypatch.setattr(idle_followup, "_run_recap_agent", fake_run_recap_agent)

    result = idle_followup._generate_recap_sync(
        321,
        [{"role": "user", "content": "最近很忙"}],
        {
            "impression": "喜欢简洁回答",
            "diary_index": [{"page": 1, "title": "Projects", "summary": "旧项目"}],
        },
    )

    response_format = captured["response_format"]
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


def test_run_recap_agent_exposes_only_read_only_memory_tools(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        idle_followup,
        "get_provider_order_for_task",
        lambda task: ["gemini"],
    )
    monkeypatch.setattr(
        idle_followup,
        "get_models_for_task",
        lambda provider, task: ["recap-model"],
    )
    monkeypatch.setattr(
        idle_followup,
        "completion_kwargs_for_task",
        lambda provider, task: {"reasoning_effort": "high"},
    )

    def fake_run_tool_loop(provider, model, messages, tool_context, **kwargs):
        captured.update(
            provider=provider,
            model=model,
            messages=messages,
            tool_context=tool_context,
            kwargs=kwargs,
            active_context=get_tool_request_context(),
        )
        return "structured result", []

    monkeypatch.setattr(idle_followup, "run_tool_loop", fake_run_tool_loop)

    response_format = {"type": "json_schema"}
    result = idle_followup._run_recap_agent(
        [{"role": "user", "content": "review"}],
        456,
        response_format,
    )

    tool_names = {
        tool["function"]["name"]
        for tool in captured["kwargs"]["tool_definitions"]
    }
    assert result == "structured result"
    assert captured["provider"] == "gemini"
    assert captured["model"] == "recap-model"
    assert captured["active_context"] == {"user_id": 456}
    assert tool_names == {
        "fetch_permanent_summaries",
        "search_permanent_records",
        "read_diary_page",
    }
    assert set(captured["kwargs"]["tool_handlers"]) == tool_names
    assert "read_diary_page" not in {
        tool["function"]["name"] for tool in idle_followup.OPENAI_TOOLS
    }
    assert captured["kwargs"]["max_tokens"] == 1000
    assert captured["kwargs"]["completion_timeout"] == 120
    assert captured["kwargs"]["completion_kwargs"] == {
        "reasoning_effort": "high",
        "response_format": response_format,
        "drop_params": False,
    }
    assert get_tool_request_context() == {}


def test_process_claim_stops_when_user_returns_during_recap(monkeypatch):
    claim = idle_followup.IdleFollowupClaim(
        user_id=123,
        activity_version=4,
        retry_count=0,
    )
    current_checks = iter([True, False])
    ai_called = []
    persisted = []

    async def fake_claim_is_current(_claim):
        return next(current_checks)

    async def fake_history(_user_id):
        return [{"role": "user", "content": "hello"}]

    async def fake_total_coins(_user_id):
        return 1

    async def fake_memory_context(_user_id):
        return {"impression": "", "diary_index": []}

    async def fake_generate(_user_id, _dialogue, _memory_context):
        return {
            "recap": "用户打了招呼",
            "open_loops": "",
            "suggested_follow_up": "",
            "memory_suggestion": {"impression": "", "diary": ""},
        }

    async def fake_ai_response(*args, **kwargs):
        ai_called.append((args, kwargs))
        return "不该发送", []

    async def fake_persist(*args, **kwargs):
        persisted.append((args, kwargs))

    monkeypatch.setattr(idle_followup, "_claim_is_current", fake_claim_is_current)
    monkeypatch.setattr(
        idle_followup,
        "_get_followup_user_total_coins",
        fake_total_coins,
    )
    monkeypatch.setattr(
        idle_followup.mysql_connection,
        "async_get_chat_history",
        fake_history,
    )
    monkeypatch.setattr(
        idle_followup,
        "_load_recap_memory_context",
        fake_memory_context,
    )
    monkeypatch.setattr(idle_followup, "_generate_recap", fake_generate)
    monkeypatch.setattr(idle_followup.ai_chat, "get_ai_response", fake_ai_response)
    monkeypatch.setattr(idle_followup, "_persist_completed_turn", fake_persist)

    asyncio.run(
        idle_followup._process_claim(
            claim,
            SimpleNamespace(bot=SimpleNamespace()),
        )
    )

    assert ai_called == []
    assert persisted == []


def test_process_claim_keeps_main_ai_tools_enabled(monkeypatch):
    claim = idle_followup.IdleFollowupClaim(
        user_id=456,
        activity_version=9,
        retry_count=0,
    )
    captured = {}
    tool_logs = [
        {
            "type": "tool_result",
            "tool_name": "read_doc",
            "arguments": {"topic": "memory"},
            "result": {"content": "memory details"},
            "tool_call_id": "call_1",
        }
    ]

    async def always_current(_claim):
        return True

    async def fake_history(_user_id):
        return [{"role": "user", "content": "hello"}]

    async def fake_total_coins(_user_id):
        return 1

    async def fake_memory_context(_user_id):
        return {"impression": "", "diary_index": []}

    async def fake_generate(_user_id, _dialogue, _memory_context):
        return {
            "recap": "用户打了招呼",
            "open_loops": "",
            "suggested_follow_up": "自然问候",
            "memory_suggestion": {
                "impression": "用户喜欢简洁回答",
                "diary": "",
            },
        }

    async def fake_user_state(_user_id):
        return "<user_state />"

    async def fake_ai_response(*args, **kwargs):
        captured["tool_context"] = kwargs["tool_context"]
        return "你好", tool_logs

    async def fake_normalize(text, *, logger):
        return text

    async def fake_persist(*args):
        captured["persist_args"] = args

    async def fake_send(*args):
        captured["send_args"] = args

    monkeypatch.setattr(idle_followup, "_claim_is_current", always_current)
    monkeypatch.setattr(
        idle_followup,
        "_get_followup_user_total_coins",
        fake_total_coins,
    )
    monkeypatch.setattr(
        idle_followup.mysql_connection,
        "async_get_chat_history",
        fake_history,
    )
    monkeypatch.setattr(
        idle_followup,
        "_load_recap_memory_context",
        fake_memory_context,
    )
    monkeypatch.setattr(idle_followup, "_generate_recap", fake_generate)
    monkeypatch.setattr(idle_followup, "build_user_state_prompt", fake_user_state)
    monkeypatch.setattr(idle_followup.ai_chat, "get_ai_response", fake_ai_response)
    monkeypatch.setattr(idle_followup, "normalize_sticker_directives", fake_normalize)
    monkeypatch.setattr(idle_followup, "_persist_completed_turn", fake_persist)
    monkeypatch.setattr(idle_followup, "_send_followup_outputs", fake_send)

    asyncio.run(
        idle_followup._process_claim(
            claim,
            SimpleNamespace(bot=SimpleNamespace()),
        )
    )

    assert "disable_tools" not in captured["tool_context"]
    assert "<memory_suggestion>" in captured["persist_args"][1]
    assert captured["persist_args"][3] == tool_logs
    assert captured["send_args"][2] == tool_logs


def test_process_claim_pauses_before_recap_when_coins_are_exhausted(monkeypatch):
    claim = idle_followup.IdleFollowupClaim(
        user_id=789,
        activity_version=3,
        retry_count=0,
    )
    paused = []
    downstream_calls = []

    async def always_current(_claim):
        return True

    async def zero_coins(_user_id):
        return 0

    async def fake_pause(_claim):
        paused.append(_claim)

    async def track_downstream(*args, **kwargs):
        downstream_calls.append((args, kwargs))
        return []

    monkeypatch.setattr(idle_followup, "_claim_is_current", always_current)
    monkeypatch.setattr(
        idle_followup,
        "_get_followup_user_total_coins",
        zero_coins,
    )
    monkeypatch.setattr(
        idle_followup,
        "_pause_claim_until_coins_available",
        fake_pause,
    )
    monkeypatch.setattr(
        idle_followup.mysql_connection,
        "async_get_chat_history",
        track_downstream,
    )
    monkeypatch.setattr(idle_followup, "_generate_recap", track_downstream)
    monkeypatch.setattr(idle_followup.ai_chat, "get_ai_response", track_downstream)
    monkeypatch.setattr(idle_followup, "_persist_completed_turn", track_downstream)
    monkeypatch.setattr(idle_followup, "_send_followup_outputs", track_downstream)

    asyncio.run(
        idle_followup._process_claim(
            claim,
            SimpleNamespace(bot=SimpleNamespace()),
        )
    )

    assert paused == [claim]
    assert downstream_calls == []
