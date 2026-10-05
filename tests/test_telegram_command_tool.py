import asyncio
from types import SimpleNamespace

import pytest

from fogmoe_telegram_bot.core import telegram_history
from fogmoe_telegram_bot.features.ai import telegram_command_executor
from fogmoe_telegram_bot.features.ai.telegram_command_executor import _execute_on_telegram_loop
from fogmoe_telegram_bot.features.ai.tools import telegram_command_tools
from fogmoe_telegram_bot.features.ai.tools.context import (
    clear_tool_request_context,
    set_tool_request_context,
)
from fogmoe_telegram_bot.features.ai.tools.models import (
    ExecuteTelegramCommandArgs,
    parameters_schema,
)
from fogmoe_telegram_bot.features.ai.tools.schemas import OPENAI_TOOLS
from fogmoe_telegram_bot.features.ai.types import TOOL_CONTEXT_MESSAGES_KEY
from fogmoe_telegram_bot.features.economy.operations.coins import give_op_key


@pytest.fixture(autouse=True)
def _clear_request_context():
    clear_tool_request_context()
    yield
    clear_tool_request_context()


def _recording_execute(calls):
    async def fake_execute(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            success=True,
            context_messages=(),
            error_code=None,
            error_message=None,
        )

    return fake_execute


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
    async def fake_execute(**kwargs):
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

    result = asyncio.run(telegram_command_tools.execute_telegram_command_tool("/me"))

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
        _recording_execute(calls),
    )

    result = asyncio.run(telegram_command_tools.execute_telegram_command_tool("/clear"))

    assert result == {"success": True}
    assert calls[0]["command"] == "clear"
    assert calls[0]["command_text"] == "/clear"


def test_unknown_command_tells_model_how_to_correct_and_retry(monkeypatch):
    monkeypatch.setattr(
        telegram_command_tools,
        "registered_telegram_commands",
        lambda: {"me", "clear"},
    )

    result = asyncio.run(telegram_command_tools.execute_telegram_command_tool("/unknown"))

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
        _recording_execute(calls),
    )

    result = asyncio.run(telegram_command_tools.execute_telegram_command_tool("/give 456 100"))

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

    async def fake_execute(**kwargs):
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

    first = asyncio.run(telegram_command_tools.execute_telegram_command_tool("/me"))
    second = asyncio.run(telegram_command_tools.execute_telegram_command_tool("/me"))

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


def test_delegated_command_runs_in_a_clean_history_context(monkeypatch):
    """命令在全新的上下文里运行：调用它的那一轮对话抑制了历史记录，命令的回复仍要被记录、被捕获。"""
    bot = SimpleNamespace(username="FogMoeBot")
    reply = SimpleNamespace(
        chat=SimpleNamespace(id=123, type="private", title=None),
        text="账户信息",
        message_id=2,
        date=None,
        reply_to_message=None,
    )

    class FakeApplication:
        async def process_update(self, update):
            # 与真实的 bot 一样，经由「记录成功发出的 bot 消息」这条路径。
            await telegram_history._record_bot_message(bot, reply)

    application = FakeApplication()
    application.bot = bot
    monkeypatch.setattr(telegram_command_executor, "_APPLICATION", application)

    async def run():
        with telegram_history.suppress_telegram_history():
            return await telegram_command_executor.execute_telegram_command(
                command="me",
                command_text="/me",
                request_context=_request_context(),
            )

    outcome = asyncio.run(run())

    assert outcome.success is True
    assert any(
        "<displayed_message>账户信息</displayed_message>" in event
        for event in outcome.context_messages
    )


def test_delegated_command_fails_cleanly_without_a_configured_application(monkeypatch):
    monkeypatch.setattr(telegram_command_executor, "_APPLICATION", None)

    outcome = asyncio.run(
        telegram_command_executor.execute_telegram_command(
            command="me",
            command_text="/me",
            request_context=_request_context(),
        )
    )

    assert outcome.success is False
    assert outcome.error_code == "execution_failed"


def _op_keys_seen_by_handlers(commands, request_context):
    """依次代执行 `commands`，返回每个 handler 算出来的 /give 扣款 op_key。"""
    seen = []

    class FakeApplication:
        async def process_update(self, update):
            seen.append(give_op_key(update.effective_chat.id, update.message.message_id))

    application = FakeApplication()
    application.bot = SimpleNamespace(username="FogMoeBot")

    async def run():
        for command_text in commands:
            await _execute_on_telegram_loop(
                application=application,
                command="give",
                command_text=command_text,
                request_context=request_context,
            )

    asyncio.run(run())
    return seen


def test_each_delegated_command_in_a_turn_gets_its_own_operation_identity():
    first, second, again = _op_keys_seen_by_handlers(
        ["/give alice 10", "/give bob 20", "/give  alice 10"],
        _request_context(),
    )

    # 合成命令复用触发对话的消息 ID，但不同的命令不会共用一个 op_key。
    assert first.startswith("give:123:88:ai:") and second.startswith("give:123:88:ai:")
    assert first != second
    # 同一轮里同样的命令（空白不同也算同一条）重放时身份不变，副作用只发生一次。
    assert again == first


def test_an_edited_message_starts_new_delegated_operations():
    original = _op_keys_seen_by_handlers(["/give alice 10"], _request_context())
    edited = _op_keys_seen_by_handlers(
        ["/give alice 10"], {**_request_context(), "message_edit_stamp": 1_780_000_000}
    )

    assert original != edited


def test_commands_typed_by_the_user_keep_the_plain_message_identity():
    assert give_op_key(123, 88) == "give:123:88"
