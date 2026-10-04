"""凭据类命令只允许私聊，以及卡密在日志和回复里只显示掩码。"""

import asyncio
import logging
from types import SimpleNamespace

import pytest
from telegram.error import Forbidden

from core import process_user, redaction, telegram_history
from features.economy import charge_coin, web_password

CODE = "123e4567-e89b-12d3-a456-426614174000"


class _FakeMessage:
    def __init__(self, delete_error=None):
        self.replies = []
        self.deleted = 0
        self.message_id = 55
        self._delete_error = delete_error

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)
        return SimpleNamespace(edit_text=self._edit_text)

    async def _edit_text(self, text, **kwargs):
        self.replies.append(text)

    async def delete(self):
        self.deleted += 1
        if self._delete_error is not None:
            raise self._delete_error


_next_user_id = iter(range(9000, 9100))


def _update(chat_type, message):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=next(_next_user_id), username="kc"),
        effective_chat=SimpleNamespace(id=-100 if chat_type != "private" else 1, type=chat_type),
        effective_message=message,
        message=message,
    )


def _context(*args):
    return SimpleNamespace(args=list(args), bot=object())


HANDLERS = [
    ("charge", charge_coin.charge_command, (CODE,)),
    ("create_code", charge_coin.admin_create_code, ("1", "10")),
    ("webpassword", web_password.webpassword_command, ("abc12345",)),
]


def test_every_private_only_command_has_a_guarded_handler():
    assert {command for command, _, _ in HANDLERS} == redaction.PRIVATE_ONLY_COMMANDS


def _fail_if_reached(monkeypatch):
    async def explode(*args, **kwargs):
        raise AssertionError("handler body must not run outside a private chat")

    monkeypatch.setattr(process_user, "async_user_exists", explode)
    monkeypatch.setattr(web_password.mysql_connection, "async_check_user_exists", explode)
    monkeypatch.setattr(web_password, "process_set_web_password", explode)


@pytest.mark.parametrize(("command", "handler", "args"), HANDLERS)
@pytest.mark.parametrize("chat_type", ["group", "supergroup"])
def test_credential_commands_are_refused_in_groups_and_message_is_deleted(
    monkeypatch,
    command,
    handler,
    args,
    chat_type,
):
    _fail_if_reached(monkeypatch)
    message = _FakeMessage()

    asyncio.run(handler(_update(chat_type, message), _context(*args)))

    assert message.deleted == 1
    assert len(message.replies) == 1
    assert f"/{command}" in message.replies[0]
    assert "私聊" in message.replies[0]
    assert all(arg not in message.replies[0] for arg in args)


@pytest.mark.parametrize(("command", "handler", "args"), HANDLERS)
def test_credential_commands_are_refused_in_other_chat_types(
    monkeypatch,
    command,
    handler,
    args,
):
    _fail_if_reached(monkeypatch)
    message = _FakeMessage()

    asyncio.run(handler(_update("channel", message), _context(*args)))

    assert message.deleted == 1
    assert "私聊" in message.replies[0]


def test_refusal_survives_missing_delete_permission_and_logs_without_credential(caplog):
    message = _FakeMessage(delete_error=Forbidden("Forbidden: not enough rights"))

    with caplog.at_level(logging.DEBUG):
        asyncio.run(
            charge_coin.charge_command(_update("supergroup", message), _context(CODE))
        )

    assert message.deleted == 1
    assert "私聊" in message.replies[0]
    assert "无法删除含凭据的命令消息" in caplog.text
    assert CODE not in caplog.text


def test_delegated_command_is_refused_without_deleting_the_source_message():
    message = _FakeMessage()

    async def run_flow():
        with telegram_history.delegated_telegram_command():
            await charge_coin.charge_command(
                _update("supergroup", message),
                _context(CODE),
            )

    asyncio.run(run_flow())

    assert message.deleted == 0
    assert "私聊" in message.replies[0]


def test_private_chat_still_reaches_the_handler(monkeypatch):
    async def not_registered(user_id):
        return False

    monkeypatch.setattr(process_user, "async_user_exists", not_registered)
    message = _FakeMessage()

    asyncio.run(charge_coin.charge_command(_update("private", message), _context(CODE)))

    assert message.deleted == 0
    assert "/me" in message.replies[0]


def test_charge_logs_and_replies_show_only_masked_code(monkeypatch, caplog):
    async def registered(user_id):
        return True

    async def redeem(user_id, code):
        return True, 50

    async def balance(user_id):
        return 150

    monkeypatch.setattr(process_user, "async_user_exists", registered)
    monkeypatch.setattr(charge_coin, "verify_and_use_code", redeem)
    monkeypatch.setattr(process_user, "async_get_user_coins", balance)
    message = _FakeMessage()

    with caplog.at_level(logging.DEBUG):
        asyncio.run(
            charge_coin.charge_command(_update("private", message), _context(CODE))
        )

    assert "充值成功" in message.replies[-1]
    assert all(CODE not in reply for reply in message.replies)
    assert "****4000" in message.replies[-1]
    assert CODE not in caplog.text
    assert "****4000" in caplog.text


def test_redemption_failure_reply_carries_reference_not_exception_text(monkeypatch, caplog):
    class _BrokenTransaction:
        async def __aenter__(self):
            raise RuntimeError(f"db failure for code {CODE}")

        async def __aexit__(self, *exc_info):
            return False

    monkeypatch.setattr(
        charge_coin.mysql_connection,
        "transaction",
        lambda: _BrokenTransaction(),
    )

    with caplog.at_level(logging.DEBUG):
        success, reason = asyncio.run(charge_coin.verify_and_use_code(7, CODE))

    assert success is False
    assert "ERR-" in reason
    assert "db failure" not in reason
    assert CODE not in caplog.text
