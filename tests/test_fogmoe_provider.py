from core import config
from features.ai.providers import fogmoe


def test_fogmoe_chat_uses_fixed_reasoning_effort(monkeypatch):
    captured = {}
    messages = [{"role": "user", "content": "hello"}]
    monkeypatch.setattr(config, "FOGMOE_CHAT_MODEL", "gpt-5.6-luna")

    def fake_run_tool_loop(provider, model, request_messages, tool_context, **kwargs):
        captured.update(
            provider=provider,
            model=model,
            messages=request_messages,
            tool_context=tool_context,
            kwargs=kwargs,
        )
        return "ok", []

    monkeypatch.setattr(fogmoe, "run_tool_loop", fake_run_tool_loop)

    assert fogmoe.get_ai_response(messages, user_id=123) == ("ok", [])
    assert captured["provider"] == "fogmoe"
    assert captured["model"] == "gpt-5.6-luna"
    assert captured["messages"] is messages
    assert captured["kwargs"]["completion_kwargs"] == {
        "reasoning_effort": "low"
    }
