from features.ai import tool_runner


class _Message:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _Choice:
    def __init__(self, message):
        self.message = message


class _Response:
    def __init__(self, message):
        self.choices = [_Choice(message)]


def test_run_tool_loop_does_not_synthesize_tool_result_reply(monkeypatch):
    responses = [
        _Response(
            _Message(
                "",
                [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "google_search",
                            "arguments": '{"query": "example"}',
                        },
                    }
                ],
            )
        ),
        _Response(_Message("", None)),
    ]

    def fake_create_chat_completion(*args, **kwargs):
        return responses.pop(0)

    monkeypatch.setattr(
        tool_runner,
        "create_chat_completion",
        fake_create_chat_completion,
    )
    monkeypatch.setitem(
        tool_runner.AI_TOOL_HANDLERS,
        "google_search",
        lambda **kwargs: {
            "organic_results": [
                {
                    "title": "Example result",
                    "link": "https://example.test",
                    "snippet": "Example snippet",
                }
            ]
        },
    )

    message, tool_logs = tool_runner.run_tool_loop(
        "test_provider",
        "test_model",
        [{"role": "user", "content": "search example"}],
        provider_name="Test",
    )

    assert message == ""
    assert any(
        log.get("type") == "tool_result"
        and log.get("tool_name") == "google_search"
        for log in tool_logs
    )
