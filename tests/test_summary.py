import json
from types import SimpleNamespace

from features.ai import summary


def test_generate_summary_counts_with_response_model(monkeypatch):
    response = SimpleNamespace(
        model="openai/gpt-4o-mini",
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="generated summary"),
            )
        ],
    )
    recorded = {}
    task_call = {}

    def fake_run_ai_task(*args, **kwargs):
        task_call.update(args=args, kwargs=kwargs)
        return response

    monkeypatch.setattr(summary, "run_ai_task", fake_run_ai_task)

    def fake_trim(value, max_tokens, *, model=None):
        recorded.update(
            value=value,
            max_tokens=max_tokens,
            model=model,
        )
        return value

    monkeypatch.setattr(summary, "_trim_summary_to_tokens", fake_trim)

    assert summary._generate_summary(123, "[]") == "generated summary"
    assert recorded == {
        "value": "generated summary",
        "max_tokens": summary.SUMMARY_MAX_TOKENS,
        "model": "openai/gpt-4o-mini",
    }
    assert task_call["kwargs"]["messages"][0] == {
        "role": "system",
        "content": summary.config.SUMMARY_SYSTEM_PROMPT,
    }


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


def test_generate_summary_falls_back_when_response_has_no_model(monkeypatch):
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="generated summary"),
            )
        ],
    )
    recorded = {}

    monkeypatch.setattr(summary, "run_ai_task", lambda *args, **kwargs: response)

    def fake_trim(value, max_tokens, *, model=None):
        recorded["model"] = model
        return value

    monkeypatch.setattr(summary, "_trim_summary_to_tokens", fake_trim)

    assert summary._generate_summary(123, "[]") == "generated summary"
    assert recorded["model"] is None


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
