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

    monkeypatch.setattr(summary, "run_ai_task", lambda *args, **kwargs: response)

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
