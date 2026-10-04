"""工具结果持久化与工具执行错误里的凭据处理。"""

import asyncio
import json
import logging

import requests

from features.ai import tool_runner
from features.ai.tool_history import tool_logs_to_record_entries

CODE = "123e4567-e89b-12d3-a456-426614174000"
SECRET = "SECRETVALUE123"


def test_tool_result_is_redacted_before_it_is_persisted():
    entries = tool_logs_to_record_entries(
        [
            {
                "type": "tool_result",
                "tool_name": "fetch_url",
                "tool_call_id": "call_1",
                "arguments": {"url": "https://x.test"},
                "result": {
                    "error": f"failed for https://x.test/?api_key={SECRET}&q=1",
                    "headers": {"Authorization": f"Bearer {SECRET}"},
                },
            }
        ]
    )

    role, record = entries[-1]
    assert role == "tool"
    assert SECRET not in record["content"]
    assert json.loads(record["content"])["error"].endswith("api_key=[redacted]&q=1")


def test_sensitive_telegram_command_arguments_are_redacted_in_persisted_tool_calls():
    arguments_json = json.dumps({"command": f"/charge {CODE}"})
    assistant_message = {
        "role": "assistant",
        "content": "好的，我来帮你充值",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "execute_telegram_command", "arguments": arguments_json},
            }
        ],
        "provider_specific_fields": {"signature": "keep-me"},
    }
    entries = tool_logs_to_record_entries(
        [
            {
                "type": "assistant_tool_call",
                "tool_name": "execute_telegram_command",
                "tool_call_id": "call_1",
                "arguments": {"command": f"/charge {CODE}"},
                "assistant_message": assistant_message,
            },
            {
                "type": "assistant_tool_call",
                "tool_name": "execute_telegram_command",
                "tool_call_id": "call_2",
                "arguments": {"command": f"/webpassword@FogBot {CODE}"},
            },
        ]
    )

    persisted = json.dumps(entries, ensure_ascii=False)
    assert CODE not in persisted
    stored = entries[0][1]
    assert json.loads(stored["tool_calls"][0]["function"]["arguments"]) == {
        "command": "/charge [redacted]"
    }
    assert stored["provider_specific_fields"] == {"signature": "keep-me"}
    # 原始消息对象不被修改，本轮后续逻辑仍使用真实参数。
    assert assistant_message["tool_calls"][0]["function"]["arguments"] == arguments_json


def test_visible_assistant_content_is_redacted_before_it_is_persisted():
    entries = tool_logs_to_record_entries(
        [
            {
                "type": "assistant_visible",
                "content": f"打开 https://x.test/?token={SECRET} 看看",
            }
        ]
    )

    assert entries == [("assistant", "打开 https://x.test/?token=[redacted] 看看")]


class _Message:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _Response:
    def __init__(self, message):
        self.choices = [type("Choice", (), {"message": message})()]


def _run_with_failing_tool(monkeypatch, error):
    tool_call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "google_search", "arguments": '{"query": "cat"}'},
    }
    responses = [_Response(_Message(None, [tool_call])), _Response(_Message("done"))]
    seen_messages = []

    async def fake_create_chat_completion(*args, **kwargs):
        seen_messages.append(list(kwargs["messages"]))
        return responses.pop(0)

    def failing_handler(**kwargs):
        raise error

    monkeypatch.setattr(tool_runner, "create_chat_completion", fake_create_chat_completion)
    monkeypatch.setitem(tool_runner.AI_TOOL_HANDLERS, "google_search", failing_handler)

    _, tool_logs = asyncio.run(tool_runner.run_tool_loop(
        "fogmoe",
        "test_model",
        [{"role": "user", "content": "search cat"}],
        provider_name="FOGMOE",
    ))
    return tool_logs, seen_messages


def test_tool_execution_error_keeps_category_but_not_credentials(monkeypatch, caplog):
    error = RuntimeError(f"upstream 429 for https://serpapi.com/search?api_key={SECRET}&q=cat")

    with caplog.at_level(logging.DEBUG):
        tool_logs, seen_messages = _run_with_failing_tool(monkeypatch, error)

    result = next(log for log in tool_logs if log["type"] == "tool_result")["result"]
    assert result["error"].startswith("执行失败: RuntimeError: upstream 429")
    assert "api_key=[redacted]" in result["error"]
    assert "(ref: ERR-" in result["error"]
    tool_message = next(m for m in seen_messages[1] if m.get("role") == "tool")
    assert SECRET not in tool_message["content"]
    assert SECRET not in caplog.text
    ref = result["error"].split("ref: ")[1].rstrip(")")
    assert f"ref={ref}" in caplog.text


def test_tool_argument_error_is_redacted_too(monkeypatch, caplog):
    error = TypeError(f"unexpected keyword password={SECRET}")

    with caplog.at_level(logging.DEBUG):
        tool_logs, _ = _run_with_failing_tool(monkeypatch, error)

    result = next(log for log in tool_logs if log["type"] == "tool_result")["result"]
    assert result["error"].startswith("参数错误: TypeError")
    assert SECRET not in result["error"]
    assert SECRET not in caplog.text


def test_tool_call_logs_do_not_expose_credential_command_arguments(monkeypatch, caplog):
    tool_call = {
        "id": "call_1",
        "type": "function",
        "function": {
            "name": "execute_telegram_command",
            "arguments": json.dumps({"command": f"/charge {CODE}"}),
        },
    }
    responses = [_Response(_Message(None, [tool_call])), _Response(_Message("done"))]
    async def fake_create_chat_completion(*args, **kwargs):
        return responses.pop(0)

    async def fake_execute_telegram_command(**kwargs):
        return {"success": True}

    monkeypatch.setattr(tool_runner, "create_chat_completion", fake_create_chat_completion)
    monkeypatch.setitem(
        tool_runner.AI_TOOL_HANDLERS,
        "execute_telegram_command",
        fake_execute_telegram_command,
    )

    with caplog.at_level(logging.DEBUG):
        asyncio.run(tool_runner.run_tool_loop(
            "fogmoe",
            "test_model",
            [{"role": "user", "content": "charge it"}],
            provider_name="FOGMOE",
        ))

    assert CODE not in caplog.text


class _FailingSession:
    def __init__(self, error):
        self.error = error

    def post(self, *args, **kwargs):
        raise self.error


def _assert_safe_tool_error(result, caplog, expected_prefix):
    assert result["error"].startswith(expected_prefix)
    assert "ConnectionError" in result["error"]
    assert "(ref: ERR-" in result["error"]
    assert SECRET not in str(result)
    assert SECRET not in caplog.text


def test_code_tool_connection_error_is_safe(monkeypatch, caplog):
    from features.ai.tools import code_tools

    error = requests.ConnectionError(f"cannot reach https://judge.test/?auth_token={SECRET}")
    monkeypatch.setattr(code_tools, "JUDGE0_API_URL", "https://judge.test")
    monkeypatch.setattr(code_tools, "_get_session", lambda: _FailingSession(error))

    with caplog.at_level(logging.DEBUG):
        result = code_tools.execute_python_code_tool("print(1)")

    _assert_safe_tool_error(result, caplog, "Failed to contact Judge0: ")


def test_image_tool_connection_error_is_safe(monkeypatch, caplog):
    from features.ai.tools import image_tools

    error = requests.ConnectionError(f"cannot reach https://img.test/?api_key={SECRET}")
    monkeypatch.setattr(image_tools, "_get_session", lambda: _FailingSession(error))

    with caplog.at_level(logging.DEBUG):
        result = image_tools._request_and_save_generated_image(
            request_items=[{"prompt": "cat"}],
            api_url="https://img.test/generate",
            api_token="image-token",
            timeout=15,
        )

    _assert_safe_tool_error(result, caplog, "Failed to contact image generation API: ")


def test_voice_tool_connection_error_is_safe(monkeypatch, caplog):
    from features.ai.tools import voice_tools

    error = requests.ConnectionError(f"cannot reach https://voice.test/?key={SECRET}")
    monkeypatch.setattr(voice_tools, "_get_session", lambda: _FailingSession(error))

    with caplog.at_level(logging.DEBUG):
        result = voice_tools._request_and_save_generated_voice(
            text="hello",
            api_key="voice-token",
            model="model",
            reference_id="reference",
            timeout=15,
        )

    _assert_safe_tool_error(result, caplog, "Failed to contact Fish Audio API: ")


def test_upstream_error_body_is_redacted_in_tool_details(monkeypatch):
    from features.ai.tools import code_tools

    class _Response:
        status_code = 401
        text = f'{{"message": "bad key", "api_key": "{SECRET}"}}'

    error = requests.HTTPError("401 Client Error", response=_Response())

    class _Session:
        def post(self, *args, **kwargs):
            return self

        def raise_for_status(self):
            raise error

    monkeypatch.setattr(code_tools, "JUDGE0_API_URL", "https://judge.test")
    monkeypatch.setattr(code_tools, "_get_session", lambda: _Session())

    result = code_tools.execute_python_code_tool("print(1)")

    assert result["status_code"] == 401
    assert SECRET not in result["details"]
