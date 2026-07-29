import json

from features.ai import summary
from features.ai.tools.context import get_tool_request_context
from features.ai.tools.schemas import OPENAI_TOOLS


def test_generate_summary_counts_with_response_model(monkeypatch):
    recorded = {}
    agent_call = {}

    def fake_run_summary_agent(messages, user_id, record_id):
        agent_call.update(
            messages=messages,
            user_id=user_id,
            record_id=record_id,
        )
        return "generated summary", "openai/gpt-4o-mini"

    monkeypatch.setattr(summary, "_run_summary_agent", fake_run_summary_agent)

    def fake_trim(value, max_tokens, *, model=None):
        recorded.update(
            value=value,
            max_tokens=max_tokens,
            model=model,
        )
        return value

    monkeypatch.setattr(summary, "_trim_summary_to_tokens", fake_trim)

    assert summary._generate_summary(123, 456, "[]", "previous summary") == (
        "generated summary"
    )
    assert recorded == {
        "value": "generated summary",
        "max_tokens": summary.SUMMARY_MAX_TOKENS,
        "model": "openai/gpt-4o-mini",
    }
    assert agent_call["user_id"] == 123
    assert agent_call["record_id"] == 456
    assert agent_call["messages"][0]["role"] == "user"
    assert "PREVIOUS_SUMMARY:\nprevious summary" in agent_call["messages"][0]["content"]
    assert "CURRENT_TRANSCRIPT:" in agent_call["messages"][0]["content"]


def test_trim_summary_passes_model_to_token_estimator(monkeypatch):
    recorded_models = []

    def fake_estimate_tokens(text, *, guard_ratio, model=None):
        recorded_models.append(model)
        return len(text)

    monkeypatch.setattr(summary, "estimate_tokens", fake_estimate_tokens)

    assert (
        summary._trim_summary_to_tokens(
            "abcdef",
            3,
            model="gemini/gemini-2.5-flash",
        )
        == "abc"
    )
    assert recorded_models
    assert set(recorded_models) == {"gemini/gemini-2.5-flash"}


def test_fetch_previous_summary_uses_only_earlier_valid_record(monkeypatch):
    captured = {}

    def fake_fetch_one(sql, params):
        captured.update(sql=sql, params=params)
        return (b" previous summary ",)

    monkeypatch.setattr(summary.mysql_connection, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(summary.mysql_connection, "run_sync", lambda value: value)

    assert summary._fetch_previous_summary(123, 456) == "previous summary"
    assert captured["params"] == (123, 456)
    assert "id < %s" in captured["sql"]
    assert "summary IS NOT NULL" in captured["sql"]


def test_run_summary_agent_exposes_only_summary_search_tool(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        summary,
        "get_provider_order_for_task",
        lambda task: ["gemini"],
    )
    monkeypatch.setattr(
        summary,
        "get_models_for_task",
        lambda provider, task: ["summary-model"],
    )
    monkeypatch.setattr(
        summary,
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
        return "generated summary", []

    monkeypatch.setattr(summary, "run_tool_loop", fake_run_tool_loop)

    result = summary._run_summary_agent(
        [{"role": "user", "content": "summarize"}],
        123,
        456,
    )

    tool_names = {
        tool["function"]["name"]
        for tool in captured["kwargs"]["tool_definitions"]
    }
    assert result == ("generated summary", "summary-model")
    assert captured["provider"] == "gemini"
    assert captured["active_context"] == {
        "user_id": 123,
        "summary_record_id": 456,
    }
    assert tool_names == {"search_prior_context"}
    assert set(captured["kwargs"]["tool_handlers"]) == tool_names
    assert "search_prior_context" not in {
        tool["function"]["name"] for tool in OPENAI_TOOLS
    }
    assert captured["kwargs"]["max_tokens"] == 2500
    assert captured["kwargs"]["context_hard_limit_ratio"] == 1.5
    assert captured["kwargs"]["max_iterations"] == 4
    assert captured["kwargs"]["completion_kwargs"] == {
        "reasoning_effort": "high"
    }
    assert get_tool_request_context() == {}


def test_format_history_labels_bot_event_as_observed_runtime_state():
    snapshot = json.dumps(
        [
            {
                "role": "user",
                "content": (
                    '<metadata type="bot_event" chat_type="private" '
                    'timestamp="2026-07-29 12:00:00" origin="bot_runtime" '
                    'event="error_notice" cause="all_ai_services_failed">\n'
                    "  <displayed_message>服务暂时不可用</displayed_message>\n"
                    "</metadata>"
                ),
            }
        ],
        ensure_ascii=False,
    )

    result = summary._format_history_for_summary(snapshot)

    assert result.startswith("BOT_EVENT:")
    assert "cause=all_ai_services_failed" in result
    assert "displayed_message=服务暂时不可用" in result
    assert "USER:" not in result


def test_format_history_labels_callback_as_user_action():
    snapshot = json.dumps(
        [
            {
                "role": "user",
                "content": (
                    '<metadata type="user_event" chat_type="private" '
                    'timestamp="2026-07-29 12:00:00" user="@kc" '
                    'origin="telegram" event="callback_query">\n'
                    '  <callback data="shop_buy_1" label="购买" />\n'
                    "</metadata>"
                ),
            }
        ],
        ensure_ascii=False,
    )

    result = summary._format_history_for_summary(snapshot)

    assert result.startswith("USER_ACTION:")
    assert "callback_data=shop_buy_1" in result
    assert "callback_label=购买" in result


def test_format_history_replaces_idle_recap_with_trigger_marker():
    snapshot = json.dumps(
        [
            {
                "role": "user",
                "content": (
                    '<metadata type="idle_followup" timestamp="2026-07-29 12:00:00" '
                    'origin="idle_recap">'
                    "<recap>model note</recap>"
                    "<open_loops>unfinished item</open_loops>"
                    "<suggested_follow_up>ask about it</suggested_follow_up>"
                    "<memory_suggestion>"
                    "<impression>likes concise replies</impression>"
                    "<diary>preparing a presentation</diary>"
                    "</memory_suggestion>"
                    "</metadata>"
                ),
            },
            {"role": "assistant", "content": "最近准备得怎么样啦？"},
        ],
        ensure_ascii=False,
    )

    result = summary._format_history_for_summary(snapshot)

    assert result == (
        "IDLE_FOLLOWUP_TRIGGER: timestamp=2026-07-29 12:00:00\n\n"
        "ASSISTANT: 最近准备得怎么样啦？"
    )
    assert "model note" not in result
    assert "unfinished item" not in result
    assert "ask about it" not in result
    assert "likes concise replies" not in result
    assert "preparing a presentation" not in result
    assert "USER:" not in result
