import pytest

from features.ai import tool_runner


@pytest.fixture(autouse=True)
def _fixed_chat_completion_timeout(monkeypatch):
    monkeypatch.setattr(
        tool_runner.config,
        "AI_CHAT_COMPLETION_TIMEOUT_SECONDS",
        300,
    )


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


def test_run_tool_loop_uses_custom_prompt_and_tool_subset(monkeypatch):
    tool_definition = {
        "type": "function",
        "function": {
            "name": "fetch_permanent_summaries",
            "description": "Fetch summaries",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    responses = [
        _Response(
            _Message(
                "",
                [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "fetch_permanent_summaries",
                            "arguments": "{}",
                        },
                    }
                ],
            )
        ),
        _Response(_Message("done", None)),
    ]
    calls = []
    global_handler_calls = []
    custom_handler_calls = []

    def fake_create_chat_completion(*args, **kwargs):
        calls.append(kwargs)
        return responses.pop(0)

    monkeypatch.setattr(tool_runner, "create_chat_completion", fake_create_chat_completion)
    monkeypatch.setitem(
        tool_runner.AI_TOOL_HANDLERS,
        "fetch_permanent_summaries",
        lambda **kwargs: global_handler_calls.append(kwargs) or {"records": []},
    )

    message, _ = tool_runner.run_tool_loop(
        "test_provider",
        "test_model",
        [{"role": "user", "content": "review"}],
        provider_name="Recap",
        tool_definitions=[tool_definition],
        tool_handlers={
            "fetch_permanent_summaries": (
                lambda **kwargs: custom_handler_calls.append(kwargs)
                or {"records": []}
            )
        },
        system_prompt_override="recap system prompt",
    )

    assert message == "done"
    assert calls[0]["tools"] == [tool_definition]
    assert calls[0]["messages"][0] == {
        "role": "system",
        "content": "recap system prompt",
    }
    assert custom_handler_calls == [{}]
    assert global_handler_calls == []


def test_run_tool_loop_rejects_tool_outside_custom_subset(monkeypatch):
    responses = [
        _Response(
            _Message(
                "",
                [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "update_impression",
                            "arguments": '{"impression":"should not run"}',
                        },
                    }
                ],
            )
        ),
        _Response(_Message("done", None)),
    ]
    calls = []
    handler_calls = []

    def fake_create_chat_completion(*args, **kwargs):
        calls.append(kwargs)
        return responses.pop(0)

    monkeypatch.setattr(tool_runner, "create_chat_completion", fake_create_chat_completion)
    monkeypatch.setitem(
        tool_runner.AI_TOOL_HANDLERS,
        "update_impression",
        lambda **kwargs: handler_calls.append(kwargs) or {"status": "updated"},
    )

    message, _ = tool_runner.run_tool_loop(
        "test_provider",
        "test_model",
        [{"role": "user", "content": "review"}],
        provider_name="Recap",
        tool_definitions=[],
        system_prompt_override="recap system prompt",
    )

    assert message == "done"
    assert handler_calls == []
    assert "Tool is not available in this agent" in str(calls[1]["messages"])


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

    calls = []

    def fake_create_chat_completion(*args, **kwargs):
        calls.append(kwargs)
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
    assert [call["timeout"] for call in calls] == [300, 300]


def test_run_tool_loop_generates_final_reply_after_tool_limit(monkeypatch):
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
        _Response(_Message("根据已有搜索结果，Example result 是相关结果。", None)),
    ]
    calls = []

    def fake_create_chat_completion(*args, **kwargs):
        calls.append(kwargs)
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
        max_iterations=1,
    )

    assert message == "根据已有搜索结果，Example result 是相关结果。"
    assert "抱歉，处理您的请求时遇到了问题" not in message
    assert len(calls) == 2
    assert "tools" in calls[0]
    assert "tool_choice" in calls[0]
    assert "tools" not in calls[1]
    assert "tool_choice" not in calls[1]
    assert calls[0]["timeout"] == 300
    assert calls[1]["timeout"] == 300
    assert "Tool calling has reached the maximum allowed iterations" not in calls[1]["messages"][0]["content"]
    assert "at most 10 tool-calling rounds" in calls[1]["messages"][0]["content"]
    assert any(message["role"] == "tool" for message in calls[1]["messages"])
    assert any(
        log.get("type") == "tool_result"
        and log.get("tool_name") == "google_search"
        for log in tool_logs
    )


def test_run_tool_loop_raises_partial_response_when_followup_times_out(monkeypatch):
    first_response = _Response(
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
    )
    calls = []
    sleeps = []

    def fake_create_chat_completion(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return first_response
        raise TimeoutError("chat completion timed out")

    monkeypatch.setattr(
        tool_runner,
        "create_chat_completion",
        fake_create_chat_completion,
    )
    monkeypatch.setattr(tool_runner.time, "sleep", sleeps.append)
    monkeypatch.setitem(
        tool_runner.AI_TOOL_HANDLERS,
        "google_search",
        lambda **kwargs: {"organic_results": []},
    )

    with pytest.raises(tool_runner.PartialAIResponseError) as exc_info:
        tool_runner.run_tool_loop(
            "test_provider",
            "test_model",
            [{"role": "user", "content": "search example"}],
            provider_name="Test",
        )

    assert [call["timeout"] for call in calls] == [300, 300, 300, 300]
    assert sleeps == list(tool_runner.POST_TOOL_COMPLETION_RETRY_DELAYS_SECONDS)
    assert any(
        log.get("type") == "tool_result"
        and log.get("tool_name") == "google_search"
        for log in exc_info.value.tool_logs
    )


def test_run_tool_loop_retries_transient_followup_without_reexecuting_tool(monkeypatch):
    first_response = _Response(
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
    )
    final_response = _Response(_Message("done", None))
    calls = []
    sleeps = []
    tool_calls = []

    class ServiceUnavailableError(Exception):
        status_code = 503

    def fake_create_chat_completion(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return first_response
        if len(calls) == 2:
            raise ServiceUnavailableError("no available accounts")
        return final_response

    monkeypatch.setattr(
        tool_runner,
        "create_chat_completion",
        fake_create_chat_completion,
    )
    monkeypatch.setattr(tool_runner.time, "sleep", sleeps.append)
    monkeypatch.setitem(
        tool_runner.AI_TOOL_HANDLERS,
        "google_search",
        lambda **kwargs: tool_calls.append(kwargs) or {"organic_results": []},
    )

    message, tool_logs = tool_runner.run_tool_loop(
        "test_provider",
        "test_model",
        [{"role": "user", "content": "search example"}],
        provider_name="Test",
    )

    assert message == "done"
    assert len(calls) == 3
    assert sleeps == [tool_runner.POST_TOOL_COMPLETION_RETRY_DELAYS_SECONDS[0]]
    assert tool_calls == [{"query": "example"}]
    assert sum(log.get("type") == "tool_result" for log in tool_logs) == 1


def test_run_tool_loop_injects_telegram_events_before_final_reply(monkeypatch):
    responses = [
        _Response(
            _Message(
                "",
                [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "execute_telegram_command",
                            "arguments": '{"command": "/me"}',
                        },
                    }
                ],
            )
        ),
        _Response(_Message("已经执行好了。", None)),
    ]
    calls = []

    def fake_create_chat_completion(*args, **kwargs):
        calls.append(kwargs)
        return responses.pop(0)

    monkeypatch.setattr(
        tool_runner,
        "create_chat_completion",
        fake_create_chat_completion,
    )
    monkeypatch.setitem(
        tool_runner.AI_TOOL_HANDLERS,
        "execute_telegram_command",
        lambda **kwargs: {
            "success": True,
            tool_runner.TOOL_CONTEXT_MESSAGES_KEY: [
                "command-event",
                "reply-event",
            ],
        },
    )

    message, tool_logs = tool_runner.run_tool_loop(
        "test_provider",
        "test_model",
        [{"role": "user", "content": "替我执行 /me"}],
        provider_name="Test",
    )

    assert message == "已经执行好了。"
    followup_messages = calls[1]["messages"]
    assert [item["role"] for item in followup_messages[-4:]] == [
        "assistant",
        "tool",
        "user",
        "user",
    ]
    assert followup_messages[-3]["content"] == '{"success": true}'
    assert followup_messages[-2:] == [
        {"role": "user", "content": "command-event"},
        {"role": "user", "content": "reply-event"},
    ]
    assert [
        log["content"]
        for log in tool_logs
        if log.get("type") == "telegram_event"
    ] == ["command-event", "reply-event"]


def test_run_tool_loop_exposes_command_error_for_model_retry(monkeypatch):
    responses = [
        _Response(
            _Message(
                "",
                [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "execute_telegram_command",
                            "arguments": '{"command": "/mee"}',
                        },
                    }
                ],
            )
        ),
        _Response(
            _Message(
                "",
                [
                    {
                        "id": "call_2",
                        "type": "function",
                        "function": {
                            "name": "execute_telegram_command",
                            "arguments": '{"command": "/me"}',
                        },
                    }
                ],
            )
        ),
        _Response(_Message("完成。", None)),
    ]
    completion_calls = []
    tool_calls = []

    def fake_create_chat_completion(*args, **kwargs):
        completion_calls.append(kwargs)
        return responses.pop(0)

    def fake_execute(**kwargs):
        tool_calls.append(kwargs)
        if kwargs["command"] == "/mee":
            return {
                "success": False,
                "error": {
                    "code": "unknown_command",
                    "message": "Use get_help_text and retry.",
                },
            }
        return {"success": True}

    monkeypatch.setattr(
        tool_runner,
        "create_chat_completion",
        fake_create_chat_completion,
    )
    monkeypatch.setitem(
        tool_runner.AI_TOOL_HANDLERS,
        "execute_telegram_command",
        fake_execute,
    )

    message, _ = tool_runner.run_tool_loop(
        "test_provider",
        "test_model",
        [{"role": "user", "content": "替我执行 /me"}],
        provider_name="Test",
    )

    assert message == "完成。"
    assert tool_calls == [{"command": "/mee"}, {"command": "/me"}]
    assert any(
        item.get("role") == "tool"
        and '"code": "unknown_command"' in str(item.get("content"))
        for item in completion_calls[1]["messages"]
    )


def test_run_tool_loop_does_not_retry_non_transient_followup_error(monkeypatch):
    first_response = _Response(
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
    )
    calls = []
    sleeps = []

    class BadRequestError(Exception):
        status_code = 400

    def fake_create_chat_completion(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return first_response
        raise BadRequestError("invalid request")

    monkeypatch.setattr(
        tool_runner,
        "create_chat_completion",
        fake_create_chat_completion,
    )
    monkeypatch.setattr(tool_runner.time, "sleep", sleeps.append)
    monkeypatch.setitem(
        tool_runner.AI_TOOL_HANDLERS,
        "google_search",
        lambda **kwargs: {"organic_results": []},
    )

    with pytest.raises(tool_runner.PartialAIResponseError):
        tool_runner.run_tool_loop(
            "test_provider",
            "test_model",
            [{"role": "user", "content": "search example"}],
            provider_name="Test",
        )

    assert len(calls) == 2
    assert sleeps == []


def test_run_tool_loop_sends_generated_voice_immediately(monkeypatch):
    responses = [
        _Response(
            _Message(
                "",
                [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "generate_voice",
                            "arguments": '{"text": "hello"}',
                        },
                    }
                ],
            )
        ),
        _Response(_Message("", None)),
    ]

    def fake_create_chat_completion(*args, **kwargs):
        return responses.pop(0)

    class _VisibleHandler:
        def __init__(self):
            self.calls = []

        def send_tool_media(self, tool_name, result):
            self.calls.append((tool_name, result))
            return ["sent_message"]

    visible_handler = _VisibleHandler()
    monkeypatch.setattr(
        tool_runner,
        "create_chat_completion",
        fake_create_chat_completion,
    )
    monkeypatch.setitem(
        tool_runner.AI_TOOL_HANDLERS,
        "generate_voice",
        lambda **kwargs: {
            "status": "generated",
            "count": 1,
            "audios": [{"audio_id": "secret-audio-id"}],
        },
    )

    message, tool_logs = tool_runner.run_tool_loop(
        "test_provider",
        "test_model",
        [{"role": "user", "content": "say hello"}],
        provider_name="Test",
        visible_content_handler=visible_handler,
    )

    voice_results = [
        log
        for log in tool_logs
        if log.get("type") == "tool_result"
        and log.get("tool_name") == "generate_voice"
    ]

    assert message == ""
    assert visible_handler.calls == [
        (
            "generate_voice",
            {
                "status": "generated",
                "count": 1,
                "audios": [{"audio_id": "secret-audio-id"}],
            },
        )
    ]
    assert voice_results[0]["media_sent"] is True
    assert voice_results[0]["sent_message_count"] == 1
    assert voice_results[0]["result"]["message"] == "Generated audio has been sent to Telegram."
    assert "forward" not in str(voice_results[0]["result"]).lower()
