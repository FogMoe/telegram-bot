import asyncio
import dataclasses

import pytest
from tool_fakes import fake_tool, use_tool

from fogmoe_telegram_bot.core import ai_providers
from fogmoe_telegram_bot.features.ai import tool_loop_messages, tool_runner
from fogmoe_telegram_bot.features.ai.agents import MAIN_AGENT
from fogmoe_telegram_bot.features.ai.types import (
    TOOL_CONTEXT_MESSAGES_KEY,
    MediaDeliveryState,
    ToolCallStatus,
)


@pytest.fixture(autouse=True)
def _fixed_chat_completion_timeout(monkeypatch):
    monkeypatch.setattr(
        tool_runner.config,
        "AI_CHAT_COMPLETION_TIMEOUT_SECONDS",
        300,
    )


class _RecordingVisibleHandler:
    """实现 `VisibleContentHandler` 的替身：原样「发出」内容并记下。"""

    def __init__(self):
        self.sent_messages = []
        self.sent_contents = []

    async def __call__(self, content):
        self.sent_contents.append(content)
        return content

    async def send_tool_media(self, tool_name, result):
        return []


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


def _response_with_choices(*messages):
    response = _Response(messages[0])
    response.choices.extend(_Choice(message) for message in messages[1:])
    return response


def test_run_tool_loop_forwards_context_hard_limit_ratio(monkeypatch):
    calls = []

    async def fake_create_chat_completion(*args, **kwargs):
        calls.append(kwargs)
        return _Response(_Message("done", None))

    monkeypatch.setattr(tool_runner, "create_chat_completion", fake_create_chat_completion)

    result = asyncio.run(tool_runner.run_tool_loop(
        "test_provider",
        "test_model",
        [{"role": "user", "content": "summarize"}],
        agent=dataclasses.replace(MAIN_AGENT, context_hard_limit_ratio=1.5),
    ))
    message = result.text

    assert message == "done"
    assert calls[0]["context_hard_limit_ratio"] == 1.5
    assert "max_tokens" not in calls[0]


def test_run_tool_loop_uses_fogmoe_tool_calls_from_later_choice(monkeypatch):
    tool_call = {
        "id": "call_1",
        "type": "function",
        "function": {
            "name": "google_search",
            "arguments": '{"query": "example"}',
        },
    }
    responses = [
        _response_with_choices(
            _Message("I will check that.", None),
            _Message(None, [tool_call]),
        ),
        _Response(_Message("done", None)),
    ]
    completion_calls = []
    handler_calls = []

    async def fake_create_chat_completion(*args, **kwargs):
        completion_calls.append(kwargs)
        return responses.pop(0)

    monkeypatch.setattr(tool_runner, "create_chat_completion", fake_create_chat_completion)
    use_tool(
        monkeypatch,
        "google_search",
        lambda **kwargs: handler_calls.append(kwargs) or {"organic_results": []},
    )

    result = asyncio.run(tool_runner.run_tool_loop(
        "fogmoe",
        "test_model",
        [{"role": "user", "content": "search example"}],
        provider_name="FOGMOE",
    ))
    message = result.text

    assert message == "done"
    assert handler_calls == [{"query": "example"}]
    assistant_message = completion_calls[1]["messages"][-2]
    assert assistant_message["role"] == "assistant"
    assert assistant_message["content"] == "I will check that."
    assert assistant_message["tool_calls"] == [tool_call]


def test_assistant_message_fallback_does_not_scan_for_other_providers():
    first_message = _Message("first answer", None)
    later_tool_calls = [{"id": "call_1"}]
    response = _response_with_choices(
        first_message,
        _Message(None, later_tool_calls),
    )

    assistant_message, tool_calls = tool_loop_messages.resolve_assistant_message(
        response,
        provider="openai",
        provider_name="OpenAI",
    )

    assert assistant_message is first_message
    assert tool_calls is None


def test_fogmoe_fallback_keeps_choice_zero_tool_calls():
    first_tool_calls = [{"id": "call_0"}]
    later_tool_calls = [{"id": "call_1"}]
    first_message = _Message("first", first_tool_calls)
    response = _response_with_choices(
        first_message,
        _Message(None, later_tool_calls),
    )

    assistant_message, tool_calls = tool_loop_messages.resolve_assistant_message(
        response,
        provider="fogmoe",
        provider_name="FOGMOE",
    )

    assert assistant_message is first_message
    assert tool_calls is first_tool_calls


def test_run_tool_loop_uses_custom_prompt_and_tool_subset(monkeypatch):
    tool_definition = {
        "type": "function",
        "function": {
            "name": "fetch_permanent_summaries",
            "description": "Fetch summaries",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    recap_agent = dataclasses.replace(
        MAIN_AGENT,
        display_name="Recap",
        system_prompt=lambda _tool_context: "recap system prompt",
    )
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

    async def fake_create_chat_completion(*args, **kwargs):
        calls.append(kwargs)
        return responses.pop(0)

    monkeypatch.setattr(tool_runner, "create_chat_completion", fake_create_chat_completion)
    use_tool(
        monkeypatch,
        "fetch_permanent_summaries",
        lambda **kwargs: global_handler_calls.append(kwargs) or {"records": []},
    )

    result = asyncio.run(tool_runner.run_tool_loop(
        "test_provider",
        "test_model",
        [{"role": "user", "content": "review"}],
        agent=recap_agent,
        tools=[
            fake_tool(
                "fetch_permanent_summaries",
                lambda **kwargs: custom_handler_calls.append(kwargs) or {"records": []},
                description="Fetch summaries",
            )
        ],
    ))
    message = result.text

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

    async def fake_create_chat_completion(*args, **kwargs):
        calls.append(kwargs)
        return responses.pop(0)

    monkeypatch.setattr(tool_runner, "create_chat_completion", fake_create_chat_completion)
    use_tool(
        monkeypatch,
        "update_impression",
        lambda **kwargs: handler_calls.append(kwargs) or {"status": "updated"},
    )

    result = asyncio.run(tool_runner.run_tool_loop(
        "test_provider",
        "test_model",
        [{"role": "user", "content": "review"}],
        agent=dataclasses.replace(
            MAIN_AGENT,
            display_name="Recap",
            system_prompt=lambda _tool_context: "recap system prompt",
        ),
        tools=[],
    ))
    message = result.text

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

    async def fake_create_chat_completion(*args, **kwargs):
        calls.append(kwargs)
        return responses.pop(0)

    monkeypatch.setattr(
        tool_runner,
        "create_chat_completion",
        fake_create_chat_completion,
    )
    use_tool(
        monkeypatch,
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

    result = asyncio.run(tool_runner.run_tool_loop(
        "test_provider",
        "test_model",
        [{"role": "user", "content": "search example"}],
        provider_name="Test",
    ))
    message = result.text

    assert message == ""
    assert any(
        record.tool_name == "google_search" and record.status is ToolCallStatus.COMPLETED
        for record in result.tool_calls
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

    async def fake_create_chat_completion(*args, **kwargs):
        calls.append(kwargs)
        return responses.pop(0)

    monkeypatch.setattr(
        tool_runner,
        "create_chat_completion",
        fake_create_chat_completion,
    )
    use_tool(
        monkeypatch,
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

    result = asyncio.run(tool_runner.run_tool_loop(
        "test_provider",
        "test_model",
        [{"role": "user", "content": "search example"}],
        provider_name="Test",
        agent=dataclasses.replace(MAIN_AGENT, max_iterations=1),
    ))
    message = result.text

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
        record.tool_name == "google_search" and record.status is ToolCallStatus.COMPLETED
        for record in result.tool_calls
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

    async def fake_create_chat_completion(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return first_response
        raise TimeoutError("chat completion timed out")

    monkeypatch.setattr(
        tool_runner,
        "create_chat_completion",
        fake_create_chat_completion,
    )
    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(tool_runner.asyncio, "sleep", fake_sleep)
    use_tool(
        monkeypatch,
        "google_search",
        lambda **kwargs: {"organic_results": []},
    )

    with pytest.raises(tool_runner.PartialAIResponseError) as exc_info:
        asyncio.run(tool_runner.run_tool_loop(
            "test_provider",
            "test_model",
            [{"role": "user", "content": "search example"}],
            provider_name="Test",
        ))

    assert [call["timeout"] for call in calls] == [300, 300, 300, 300]
    assert sleeps == list(tool_runner.POST_TOOL_COMPLETION_RETRY_DELAYS_SECONDS)
    assert any(
        record.tool_name == "google_search" and record.status is ToolCallStatus.COMPLETED
        for record in exc_info.value.partial.tool_calls
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

    async def fake_create_chat_completion(*args, **kwargs):
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
    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(tool_runner.asyncio, "sleep", fake_sleep)
    use_tool(
        monkeypatch,
        "google_search",
        lambda **kwargs: tool_calls.append(kwargs) or {"organic_results": []},
    )

    result = asyncio.run(tool_runner.run_tool_loop(
        "test_provider",
        "test_model",
        [{"role": "user", "content": "search example"}],
        provider_name="Test",
    ))
    message = result.text

    assert message == "done"
    assert len(calls) == 3
    assert sleeps == [tool_runner.POST_TOOL_COMPLETION_RETRY_DELAYS_SECONDS[0]]
    assert tool_calls == [{"query": "example"}]
    assert [record.status for record in result.tool_calls] == [ToolCallStatus.COMPLETED]


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

    async def fake_create_chat_completion(*args, **kwargs):
        calls.append(kwargs)
        return responses.pop(0)

    monkeypatch.setattr(
        tool_runner,
        "create_chat_completion",
        fake_create_chat_completion,
    )
    use_tool(
        monkeypatch,
        "execute_telegram_command",
        lambda **kwargs: {
            "success": True,
            TOOL_CONTEXT_MESSAGES_KEY: [
                "command-event",
                "reply-event",
            ],
        },
    )

    result = asyncio.run(tool_runner.run_tool_loop(
        "test_provider",
        "test_model",
        [{"role": "user", "content": "替我执行 /me"}],
        provider_name="Test",
    ))
    message = result.text

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
        message["content"]
        for message in result.turn_messages
        if message["role"] == "user"
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

    async def fake_create_chat_completion(*args, **kwargs):
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
    use_tool(
        monkeypatch,
        "execute_telegram_command",
        fake_execute,
    )

    result = asyncio.run(tool_runner.run_tool_loop(
        "test_provider",
        "test_model",
        [{"role": "user", "content": "替我执行 /me"}],
        provider_name="Test",
    ))
    message = result.text

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

    async def fake_create_chat_completion(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return first_response
        raise BadRequestError("invalid request")

    monkeypatch.setattr(
        tool_runner,
        "create_chat_completion",
        fake_create_chat_completion,
    )
    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(tool_runner.asyncio, "sleep", fake_sleep)
    use_tool(
        monkeypatch,
        "google_search",
        lambda **kwargs: {"organic_results": []},
    )

    with pytest.raises(tool_runner.PartialAIResponseError):
        asyncio.run(tool_runner.run_tool_loop(
            "test_provider",
            "test_model",
            [{"role": "user", "content": "search example"}],
            provider_name="Test",
        ))

    assert len(calls) == 2
    assert sleeps == []


def _search_call(call_id="call_1"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": "google_search", "arguments": '{"query": "example"}'},
    }


class _NoChoices:
    choices = []


def test_an_error_after_a_tool_round_leaves_the_loop_as_partial(monkeypatch):
    responses = [_Response(_Message("", [_search_call()])), _NoChoices()]
    tool_calls = []

    async def fake_create_chat_completion(*args, **kwargs):
        return responses.pop(0)

    monkeypatch.setattr(tool_runner, "create_chat_completion", fake_create_chat_completion)
    use_tool(
        monkeypatch,
        "google_search",
        lambda **kwargs: tool_calls.append(kwargs) or {"organic_results": []},
    )

    with pytest.raises(tool_runner.PartialAIResponseError) as exc_info:
        asyncio.run(tool_runner.run_tool_loop(
            "test_provider",
            "test_model",
            [{"role": "user", "content": "search example"}],
            provider_name="Test",
        ))

    assert tool_calls == [{"query": "example"}]
    assert [
        (record.tool_name, record.status) for record in exc_info.value.partial.tool_calls
    ] == [("google_search", ToolCallStatus.COMPLETED)]
    assert "without choices" in str(exc_info.value)


def test_an_error_before_any_side_effect_is_raised_unchanged(monkeypatch):
    async def fake_create_chat_completion(*args, **kwargs):
        return _NoChoices()

    monkeypatch.setattr(tool_runner, "create_chat_completion", fake_create_chat_completion)

    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(tool_runner.run_tool_loop(
            "test_provider",
            "test_model",
            [{"role": "user", "content": "hi"}],
            provider_name="Test",
        ))

    assert not isinstance(exc_info.value, tool_runner.PartialAIResponseError)


def test_visible_content_alone_makes_a_later_error_partial(monkeypatch):
    class BadRequestError(Exception):
        status_code = 400

    async def fake_create_chat_completion(*args, **kwargs):
        return _Response(_Message("我先想想。", [_search_call()]))

    visible_handler = _RecordingVisibleHandler()

    def broken_plain(*args, **kwargs):
        raise BadRequestError("invalid request")

    monkeypatch.setattr(tool_runner, "create_chat_completion", fake_create_chat_completion)
    monkeypatch.setattr(tool_runner, "assistant_message_to_plain", broken_plain)

    with pytest.raises(tool_runner.PartialAIResponseError) as exc_info:
        asyncio.run(tool_runner.run_tool_loop(
            "test_provider",
            "test_model",
            [{"role": "user", "content": "hi"}],
            provider_name="Test",
            visible_content_handler=visible_handler,
        ))

    assert exc_info.value.partial.delivered_contents == ("我先想想。",)
    assert exc_info.value.partial.tool_calls == ()


def test_skip_tools_hides_schema_and_pairs_error_when_called_anyway(monkeypatch):
    responses = [
        _Response(_Message("", [_search_call("call_skipped")])),
        _Response(_Message("fallback answer", None)),
    ]
    calls = []
    handler_calls = []

    async def fake_create_chat_completion(*args, **kwargs):
        calls.append(kwargs)
        return responses.pop(0)

    monkeypatch.setattr(tool_runner, "create_chat_completion", fake_create_chat_completion)
    use_tool(
        monkeypatch,
        "google_search",
        lambda **kwargs: handler_calls.append(kwargs) or {"organic_results": []},
    )
    hiding_provider = dataclasses.replace(
        ai_providers.require("openai"),
        skip_tools=("google_search",),
    )
    monkeypatch.setattr(tool_runner.ai_providers, "lookup", lambda _name: hiding_provider)

    result = asyncio.run(tool_runner.run_tool_loop(
        "openai",
        "test_model",
        [{"role": "user", "content": "search example"}],
        provider_name="Test",
    ))
    message = result.text

    advertised_names = {tool["function"]["name"] for tool in calls[0]["tools"]}
    assert "google_search" not in advertised_names
    assert handler_calls == []
    assert message == "fallback answer"

    followup_messages = calls[1]["messages"]
    assistant_msg = next(m for m in followup_messages if m.get("tool_calls"))
    tool_msg = next(m for m in followup_messages if m.get("role") == "tool")
    assert assistant_msg["tool_calls"][0]["id"] == "call_skipped"
    assert tool_msg["tool_call_id"] == "call_skipped"
    assert "Tool is not available in this agent: google_search" in tool_msg["content"]
    assert [record.status for record in result.tool_calls] == [ToolCallStatus.REJECTED]


def test_missing_function_name_and_id_still_pairs_tool_call_and_result(monkeypatch):
    responses = [
        _Response(
            _Message(
                "",
                [{"type": "function", "function": {"name": "", "arguments": "{}"}}],
            )
        ),
        _Response(_Message("recovered", None)),
    ]
    calls = []

    async def fake_create_chat_completion(*args, **kwargs):
        calls.append(kwargs)
        return responses.pop(0)

    monkeypatch.setattr(tool_runner, "create_chat_completion", fake_create_chat_completion)

    result = asyncio.run(tool_runner.run_tool_loop(
        "test_provider",
        "test_model",
        [{"role": "user", "content": "hi"}],
        provider_name="Test",
    ))
    message = result.text

    assert message == "recovered"
    followup_messages = calls[1]["messages"]
    assistant_msg = next(m for m in followup_messages if m.get("tool_calls"))
    tool_msg = next(m for m in followup_messages if m.get("role") == "tool")
    generated_id = assistant_msg["tool_calls"][0]["id"]
    assert generated_id.startswith("call_")
    assert tool_msg["tool_call_id"] == generated_id
    assert "Tool call is missing function name" in tool_msg["content"]
    assert [record.tool_call_id for record in result.tool_calls] == [generated_id]


def test_generated_tool_call_ids_stay_paired_across_turns(monkeypatch):
    """provider 不给 id 时，每一轮生成的 id 都不同：两轮的消息拼进同一段历史后，写库时的配对清洗
    不会把后一轮的 tool 结果当成重复删掉。"""
    from fogmoe_telegram_bot.core.chat_records import _sanitize_messages_with_tool_pairs

    def one_turn():
        responses = [
            _Response(
                _Message(
                    "",
                    [{"type": "function", "function": {"name": "read_doc", "arguments": "{}"}}],
                )
            ),
            _Response(_Message("done", None)),
        ]

        async def fake_create_chat_completion(*args, **kwargs):
            return responses.pop(0)

        monkeypatch.setattr(tool_runner, "create_chat_completion", fake_create_chat_completion)
        use_tool(monkeypatch, "read_doc", lambda **kwargs: {"ok": True})
        result = asyncio.run(tool_runner.run_tool_loop(
            "test_provider",
            "test_model",
            [{"role": "user", "content": "hi"}],
            provider_name="Test",
        ))
        return result.turn_messages

    history = one_turn() + one_turn()

    sanitized, changed = _sanitize_messages_with_tool_pairs(history)

    assert changed is False
    assert [message["role"] for message in sanitized] == ["assistant", "tool", "assistant", "tool"]


def test_an_unserializable_tool_result_reaches_the_model_as_text(monkeypatch):
    responses = [_Response(_Message("", [_search_call()])), _Response(_Message("done", None))]
    calls = []
    tool_calls = []

    class Opaque:
        def __str__(self):
            return "opaque-value"

    async def fake_create_chat_completion(*args, **kwargs):
        calls.append(kwargs)
        return responses.pop(0)

    monkeypatch.setattr(tool_runner, "create_chat_completion", fake_create_chat_completion)
    use_tool(
        monkeypatch,
        "google_search",
        lambda **kwargs: tool_calls.append(kwargs) or {"value": Opaque()},
    )

    result = asyncio.run(tool_runner.run_tool_loop(
        "test_provider",
        "test_model",
        [{"role": "user", "content": "search example"}],
        provider_name="Test",
    ))
    message = result.text

    assert message == "done"
    assert tool_calls == [{"query": "example"}]
    tool_message = next(item for item in calls[1]["messages"] if item.get("role") == "tool")
    assert tool_message["content"] == '{"value": "opaque-value"}'


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

    async def fake_create_chat_completion(*args, **kwargs):
        return responses.pop(0)

    class _VisibleHandler(_RecordingVisibleHandler):
        def __init__(self):
            super().__init__()
            self.calls = []

        async def send_tool_media(self, tool_name, result):
            self.calls.append((tool_name, result))
            return ["sent_message"]

    visible_handler = _VisibleHandler()
    monkeypatch.setattr(
        tool_runner,
        "create_chat_completion",
        fake_create_chat_completion,
    )
    use_tool(
        monkeypatch,
        "generate_voice",
        lambda **kwargs: {
            "status": "generated",
            "count": 1,
            "audios": [{"audio_id": "secret-audio-id"}],
        },
    )

    result = asyncio.run(tool_runner.run_tool_loop(
        "test_provider",
        "test_model",
        [{"role": "user", "content": "say hello"}],
        provider_name="Test",
        visible_content_handler=visible_handler,
    ))
    message = result.text

    voice_results = [
        record for record in result.tool_calls if record.tool_name == "generate_voice"
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
    assert voice_results[0].media_delivery is MediaDeliveryState.SENT
    assert voice_results[0].public_result["message"] == "Generated audio has been sent to Telegram."
    assert "forward" not in str(voice_results[0].public_result).lower()


def test_tool_loop_logs_its_token_totals(monkeypatch, caplog):
    from types import SimpleNamespace

    def with_usage(response):
        response.usage = SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=20,
            prompt_tokens_details=SimpleNamespace(cached_tokens=60),
        )
        return response

    responses = [
        with_usage(
            _Response(
                _Message(
                    "",
                    [{"id": "c1", "type": "function", "function": {"name": "read_doc", "arguments": "{}"}}],
                )
            )
        ),
        with_usage(_Response(_Message("done", None))),
    ]

    async def fake_create_chat_completion(*args, **kwargs):
        return responses.pop(0)

    monkeypatch.setattr(tool_runner, "create_chat_completion", fake_create_chat_completion)
    use_tool(monkeypatch, "read_doc", lambda **kwargs: {"ok": True})

    with caplog.at_level("INFO"):
        asyncio.run(tool_runner.run_tool_loop(
            "test_provider",
            "test_model",
            [{"role": "user", "content": "hi"}],
            {"user_id": 7},
            provider_name="Test",
        ))

    (line,) = [r.getMessage() for r in caplog.records if "tool loop finished" in r.getMessage()]
    assert line == (
        "Test tool loop finished: model=test_model user_id=7 ai_calls=2 in_tokens=200 "
        "cached_tokens=120 out_tokens=40 tool_calls=1"
    )


class _NormalizingVisibleHandler(_RecordingVisibleHandler):
    """像真实处理器一样：`[no_response]` 规范化后没有可发的内容，返回空串、什么都不发；`fail` 里的内容整条发送失败。"""

    def __init__(self, fail=()):
        super().__init__()
        self.fail = set(fail)

    async def __call__(self, content):
        if content in self.fail:
            raise RuntimeError("telegram send failed")
        if content.strip() == "[no_response]":
            return ""
        return await super().__call__(content)


@pytest.mark.parametrize(
    ("second_content", "fail"),
    [
        ("[no_response]", ()),  # 规范化后没有可发的内容
        ("这句发不出去", ("这句发不出去",)),  # 整条发送失败，什么都没发出
    ],
)
def test_a_later_visible_send_that_sends_nothing_does_not_replay_the_earlier_one(
    monkeypatch, second_content, fail
):
    responses = [
        _Response(_Message("我先查一下。", [_search_call("call_1")])),
        _Response(_Message(second_content, [_search_call("call_2")])),
        _Response(_Message("", None)),
    ]
    handler_calls = []

    async def fake_create_chat_completion(*args, **kwargs):
        return responses.pop(0)

    monkeypatch.setattr(tool_runner, "create_chat_completion", fake_create_chat_completion)
    use_tool(
        monkeypatch,
        "google_search",
        lambda **kwargs: handler_calls.append(kwargs) or {"organic_results": []},
    )

    result = asyncio.run(tool_runner.run_tool_loop(
        "test_provider",
        "test_model",
        [{"role": "user", "content": "search"}],
        provider_name="Test",
        visible_content_handler=_NormalizingVisibleHandler(fail),
    ))

    # 第二轮什么都没发出：不把第一轮发过的内容当成这次「部分发出」再记一遍，第二轮的工具照常执行。
    assert result.delivered_contents == ("我先查一下。",)
    assert len(handler_calls) == 2
    assert [record.tool_call_id for record in result.tool_calls] == ["call_1", "call_2"]
    assert [
        message["content"]
        for message in result.turn_messages
        if message["role"] == "assistant" and not message.get("tool_calls")
    ] == []
