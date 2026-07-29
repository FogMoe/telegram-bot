import asyncio
from types import SimpleNamespace

import pytest

from core import telegram_history
from features.ai.telegram_command_executor import _execute_on_telegram_loop
from features.ai.tools import telegram_command_tools
from features.ai.tools.context import (
    clear_tool_request_context,
    set_tool_request_context,
)
from features.ai.tools.models import ExecuteTelegramCommandArgs, parameters_schema
from features.ai.tools.schemas import OPENAI_TOOLS
from features.ai.types import TOOL_CONTEXT_MESSAGES_KEY


@pytest.fixture(autouse=True)
def _clear_request_context():
    clear_tool_request_context()
    yield
    clear_tool_request_context()


def _request_context():
    return {
        "user_id": 123,
        "chat_id": 123,
        "chat_type": "private",
        "message_id": 88,
        "username": "kc",
        "first_name": "Kc",
    }


def test_execute_telegram_command_schema_accepts_one_complete_command():
    schema = parameters_schema(ExecuteTelegramCommandArgs)
    tool_names = [tool["function"]["name"] for tool in OPENAI_TOOLS]

    assert set(schema["properties"]) == {"command"}
    assert "enum" not in schema["properties"]["command"]
    assert "including the leading slash" in schema["properties"]["command"][
        "description"
    ]
    assert "execute_telegram_command" in tool_names


def test_supported_command_returns_only_status_plus_internal_context(monkeypatch):
    set_tool_request_context(_request_context())
    monkeypatch.setattr(
        telegram_command_tools,
        "registered_telegram_commands",
        lambda: {"me", "clear"},
    )
    monkeypatch.setattr(
        telegram_command_tools,
        "execute_telegram_command",
        lambda **kwargs: SimpleNamespace(
            success=True,
            context_messages=("command-event", "reply-event"),
            error_code=None,
            error_message=None,
        ),
    )

    result = telegram_command_tools.execute_telegram_command_tool("/me")

    assert result == {
        "success": True,
        TOOL_CONTEXT_MESSAGES_KEY: ["command-event", "reply-event"],
    }


def test_any_registered_command_can_be_executed(monkeypatch):
    set_tool_request_context(_request_context())
    monkeypatch.setattr(
        telegram_command_tools,
        "registered_telegram_commands",
        lambda: {"me", "clear"},
    )
    calls = []
    monkeypatch.setattr(
        telegram_command_tools,
        "execute_telegram_command",
        lambda **kwargs: calls.append(kwargs)
        or SimpleNamespace(
            success=True,
            context_messages=(),
            error_code=None,
            error_message=None,
        ),
    )

    result = telegram_command_tools.execute_telegram_command_tool("/clear")

    assert result == {"success": True}
    assert calls[0]["command"] == "clear"
    assert calls[0]["command_text"] == "/clear"


def test_unknown_command_tells_model_how_to_correct_and_retry(monkeypatch):
    monkeypatch.setattr(
        telegram_command_tools,
        "registered_telegram_commands",
        lambda: {"me", "clear"},
    )

    result = telegram_command_tools.execute_telegram_command_tool("/unknown")

    assert result["error"]["code"] == "unknown_command"
    assert "Use get_help_text" in result["error"]["message"]
    assert "retry with the corrected complete command" in result["error"]["message"]


def test_complete_command_arguments_are_forwarded_to_handler(monkeypatch):
    set_tool_request_context(_request_context())
    monkeypatch.setattr(
        telegram_command_tools,
        "registered_telegram_commands",
        lambda: {"give"},
    )
    calls = []
    monkeypatch.setattr(
        telegram_command_tools,
        "execute_telegram_command",
        lambda **kwargs: calls.append(kwargs)
        or SimpleNamespace(
            success=True,
            context_messages=(),
            error_code=None,
            error_message=None,
        ),
    )

    result = telegram_command_tools.execute_telegram_command_tool("/give 456 100")

    assert result == {"success": True}
    assert calls[0]["command"] == "give"
    assert calls[0]["command_text"] == "/give 456 100"


def test_same_command_is_not_executed_twice_in_one_ai_request(monkeypatch):
    set_tool_request_context(_request_context())
    monkeypatch.setattr(
        telegram_command_tools,
        "registered_telegram_commands",
        lambda: {"me"},
    )
    calls = []

    def fake_execute(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            success=True,
            context_messages=("command-event", "reply-event"),
            error_code=None,
            error_message=None,
        )

    monkeypatch.setattr(
        telegram_command_tools,
        "execute_telegram_command",
        fake_execute,
    )

    first = telegram_command_tools.execute_telegram_command_tool("/me")
    second = telegram_command_tools.execute_telegram_command_tool("/me")

    assert len(calls) == 1
    assert TOOL_CONTEXT_MESSAGES_KEY in first
    assert second == {"success": True}


def test_executor_captures_delegated_command_and_mechanical_reply():
    bot = SimpleNamespace(username="FogMoeBot")

    class FakeApplication:
        async def process_update(self, update):
            await telegram_history.prepare_update_history(
                update,
                SimpleNamespace(bot=bot),
            )
            await telegram_history._persist_event(
                update.effective_user.id,
                telegram_history.format_bot_event(
                    chat_type="private",
                    chat_title=None,
                    timestamp="2026-07-29 12:00:01",
                    origin="command_handler",
                    event="command_reply",
                    command="me",
                    displayed_message="账户信息",
                ),
                bot,
            )

    application = FakeApplication()
    application.bot = bot

    outcome = asyncio.run(
        _execute_on_telegram_loop(
            application=application,
            command="me",
            command_text="/me",
            request_context=_request_context(),
        )
    )

    assert outcome.success is True
    assert len(outcome.context_messages) == 2
    assert 'origin="ai_tool"' in outcome.context_messages[0]
    assert 'delegated="true"' in outcome.context_messages[0]
    assert "<message>/me</message>" in outcome.context_messages[0]
    assert 'type="bot_event"' in outcome.context_messages[1]
    assert "<displayed_message>账户信息</displayed_message>" in outcome.context_messages[1]
