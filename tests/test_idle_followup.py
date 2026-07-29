import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from features.ai import idle_followup


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
        '"suggested_follow_up":"问进展"}'
    )
    assert result == {
        "recap": "聊了计划",
        "open_loops": "确认时间",
        "suggested_follow_up": "问进展",
    }

    with pytest.raises(ValueError, match="required JSON schema"):
        idle_followup._parse_recap_response(
            '{"recap": [], "open_loops": "", "suggested_follow_up": ""}'
        )

    with pytest.raises(ValueError, match="required JSON schema"):
        idle_followup._parse_recap_response(
            '```json\n{"recap":"x","open_loops":"","suggested_follow_up":""}\n```'
        )


def test_format_idle_recap_event_escapes_output_without_internal_version():
    result = idle_followup._format_idle_recap_event(
        {
            "recap": "用户提到 <计划>",
            "open_loops": "确认 & 回复",
            "suggested_follow_up": "问一下进展",
        },
        timestamp=datetime(2026, 7, 29, 12, 0, 0),
    )

    assert 'origin="idle_recap"' in result
    assert "chat_type" not in result
    assert "activity_version" not in result
    assert "<recap>用户提到 &lt;计划&gt;</recap>" in result
    assert "<open_loops>确认 &amp; 回复</open_loops>" in result
    assert "<message>" not in result
    assert "<instruction>" not in result


def test_generate_recap_requests_strict_sdk_json_schema(monkeypatch):
    captured = {}
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=(
                        '{"recap":"聊了计划","open_loops":"",'
                        '"suggested_follow_up":"稍后问候"}'
                    )
                )
            )
        ]
    )

    def fake_run_ai_task(task, messages, **kwargs):
        captured.update(task=task, messages=messages, kwargs=kwargs)
        return response

    monkeypatch.setattr(idle_followup, "run_ai_task", fake_run_ai_task)

    result = idle_followup._generate_recap_sync(
        [{"role": "user", "content": "最近很忙"}]
    )

    response_format = captured["kwargs"]["response_format"]
    assert result["recap"] == "聊了计划"
    assert captured["task"] == "recap"
    assert captured["kwargs"]["drop_params"] is False
    assert captured["kwargs"]["max_tokens"] == 1000
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    assert response_format["json_schema"]["schema"]["additionalProperties"] is False


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

    async def fake_generate(_dialogue):
        return {
            "recap": "用户打了招呼",
            "open_loops": "",
            "suggested_follow_up": "",
        }

    async def fake_ai_response(*args, **kwargs):
        ai_called.append((args, kwargs))
        return "不该发送", []

    async def fake_persist(*args, **kwargs):
        persisted.append((args, kwargs))

    monkeypatch.setattr(idle_followup, "_claim_is_current", fake_claim_is_current)
    monkeypatch.setattr(
        idle_followup.mysql_connection,
        "async_get_chat_history",
        fake_history,
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

    async def fake_generate(_dialogue):
        return {
            "recap": "用户打了招呼",
            "open_loops": "",
            "suggested_follow_up": "自然问候",
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
        idle_followup.mysql_connection,
        "async_get_chat_history",
        fake_history,
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
    assert captured["persist_args"][3] == tool_logs
    assert captured["send_args"][2] == tool_logs
