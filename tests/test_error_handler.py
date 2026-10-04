import asyncio
import logging
from types import SimpleNamespace

from telegram import Message, Update

from app import error_handler as error_handler_module

SECRET = "SECRETVALUE123"
PRIVATE_TEXT = "my private message body"


def _real_update():
    return Update.de_json(
        {
            "update_id": 7001,
            "message": {
                "message_id": 5,
                "date": 1_700_000_000,
                "chat": {"id": 42, "type": "private", "first_name": "Kc"},
                "from": {"id": 42, "is_bot": False, "first_name": "Kc"},
                "text": PRIVATE_TEXT,
            },
        },
        None,
    )


class _Recorder:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


def _context(error):
    sent = []

    async def send_message(**kwargs):
        sent.append(kwargs)

    return SimpleNamespace(
        error=error,
        bot=SimpleNamespace(send_message=send_message),
        sent=sent,
    )


def test_user_reply_has_reference_but_not_exception_text(caplog):
    recorder = _Recorder()
    update = SimpleNamespace(
        update_id=9,
        effective_message=recorder,
        callback_query=None,
        effective_chat=SimpleNamespace(id=42, type="private"),
        effective_user=SimpleNamespace(id=42),
    )
    error = RuntimeError(f"db down at https://x.test/?api_key={SECRET} password=hunter2")

    with caplog.at_level(logging.DEBUG):
        asyncio.run(error_handler_module.error_handler(update, _context(error)))

    reply = recorder.replies[0]
    assert "ERR-" in reply
    assert "db down" not in reply
    assert SECRET not in reply
    assert "RuntimeError" not in reply
    assert SECRET not in caplog.text
    assert "hunter2" not in caplog.text
    ref = reply.split("ERR-")[1][:8]
    assert f"ref=ERR-{ref}" in caplog.text


def test_log_has_identifiers_but_not_the_full_update(monkeypatch, caplog):
    update = _real_update()
    replies = []

    async def fake_reply_text(self, text, **kwargs):
        replies.append(text)

    monkeypatch.setattr(Message, "reply_text", fake_reply_text)

    with caplog.at_level(logging.DEBUG):
        asyncio.run(
            error_handler_module.error_handler(update, _context(RuntimeError("boom")))
        )

    assert replies and "ERR-" in replies[0]
    assert "update_id=7001" in caplog.text
    assert "update_kind=message" in caplog.text
    assert "chat_id=42" in caplog.text
    assert "user_id=42" in caplog.text
    assert PRIVATE_TEXT not in caplog.text
    assert "Update(" not in caplog.text


def test_callback_query_failure_notice_has_no_exception_text():
    answers = []

    async def answer(text):
        answers.append(text)

    update = SimpleNamespace(
        update_id=10,
        effective_message=None,
        callback_query=SimpleNamespace(answer=answer),
        effective_chat=SimpleNamespace(id=42, type="private"),
        effective_user=SimpleNamespace(id=42),
    )
    context = _context(RuntimeError(f"token={SECRET}"))

    asyncio.run(error_handler_module.error_handler(update, context))

    assert answers == ["处理请求时出错，请稍后再试"]
    assert "ERR-" in context.sent[0]["text"]
    assert SECRET not in context.sent[0]["text"]


def test_error_handler_logs_failure_to_notify_user_without_secret(caplog):
    class _BrokenReply:
        async def reply_text(self, text, **kwargs):
            raise RuntimeError(f"cannot send https://x.test/?token={SECRET}")

    update = SimpleNamespace(
        update_id=11,
        effective_message=_BrokenReply(),
        callback_query=None,
        effective_chat=None,
        effective_user=None,
    )

    with caplog.at_level(logging.DEBUG):
        asyncio.run(error_handler_module.error_handler(update, _context(ValueError("x"))))

    assert "在处理错误时又发生了错误" in caplog.text
    assert SECRET not in caplog.text
