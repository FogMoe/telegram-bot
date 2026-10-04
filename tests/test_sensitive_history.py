"""凭据类命令在个人历史与群聊历史两条存储路径上的统一脱敏。"""

import asyncio
import base64
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from core import group_chat_history, redaction, telegram_history

SECRET = "top-secret-0001"


@pytest.fixture(autouse=True)
def _clear_pending_history_events():
    telegram_history._PENDING_EVENTS.clear()
    yield
    for task in telegram_history._PENDING_FLUSH_TASKS.values():
        task.cancel()
    telegram_history._PENDING_FLUSH_TASKS.clear()
    telegram_history._PENDING_EVENTS.clear()
    telegram_history._PENDING_FLUSH_LOCKS.clear()


@pytest.fixture
def stores(monkeypatch):
    """替换两条存储出口，记录最终写入的内容。"""
    personal: list[str] = []
    group: list[tuple] = []

    async def fake_persist(user_id, content, bot):
        personal.append(content)

    async def fake_group_write(record):
        group.append(record)

    monkeypatch.setattr(telegram_history, "_persist_event", fake_persist)
    monkeypatch.setattr(group_chat_history, "_log_group_message", fake_group_write)
    return SimpleNamespace(personal=personal, group=group)


def _message(text, *, chat, message_id=88, from_id=123):
    return SimpleNamespace(
        text=text,
        caption=None,
        photo=None,
        sticker=None,
        animation=None,
        document=None,
        video=None,
        audio=None,
        voice=None,
        video_note=None,
        poll=None,
        venue=None,
        location=None,
        contact=None,
        dice=None,
        reply_to_message=None,
        chat=chat,
        from_user=SimpleNamespace(id=from_id),
        date=datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc),
        message_id=message_id,
    )


def _update(text, chat):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=123, username="kc"),
        effective_chat=chat,
        effective_message=_message(text, chat=chat),
        edited_message=None,
        callback_query=None,
    )


def _group_content(record):
    _group_id, _message_id, _user_id, message_type, content, _created_at = record
    if message_type == "text":
        return content
    return base64.b64decode(content).decode("utf-8")


@pytest.mark.parametrize(
    "command_text",
    [
        f"/charge {SECRET}",
        f"/CHARGE@FogMoeBot {SECRET}",
        f"/webpassword {SECRET}",
        f"/webpassword@FogMoeBot {SECRET}",
    ],
)
def test_sensitive_command_is_absent_from_personal_and_group_history(
    stores,
    command_text,
):
    chat = SimpleNamespace(id=-100, type="supergroup", title="Group")

    asyncio.run(telegram_history.record_command_update(_update(command_text, chat), object()))

    assert len(stores.personal) == 1
    assert SECRET not in stores.personal[0]
    assert 'redacted="true"' in stores.personal[0]
    assert len(stores.group) == 1
    group_text = _group_content(stores.group[0])
    assert SECRET not in group_text
    assert group_text.endswith("[redacted]")


def test_sensitive_command_in_private_chat_stays_out_of_group_history(stores):
    chat = SimpleNamespace(id=123, type="private", title=None)

    asyncio.run(
        telegram_history.record_command_update(_update(f"/charge {SECRET}", chat), object())
    )

    assert SECRET not in stores.personal[0]
    assert stores.group == []


@pytest.mark.parametrize("command", ["charge", "webpassword"])
def test_bot_reply_to_sensitive_command_is_redacted_in_both_histories(stores, command):
    chat = SimpleNamespace(id=-100, type="supergroup", title="Group")
    update = _update(f"/{command} {SECRET}", chat)

    async def run_flow():
        await telegram_history.prepare_update_history(
            update,
            SimpleNamespace(bot=object()),
        )
        await telegram_history._record_bot_message(
            object(),
            _message(f"处理 {SECRET} 失败", chat=chat, message_id=89, from_id=999),
        )

    asyncio.run(run_flow())

    reply_event = stores.personal[-1]
    assert SECRET not in reply_event
    assert "处理 [redacted] 失败" in reply_event
    reply_group_text = _group_content(stores.group[-1])
    assert SECRET not in reply_group_text
    assert reply_group_text == "处理 [redacted] 失败"
    # 命令本身也已写入群聊历史，同样不含原值。
    assert all(SECRET not in _group_content(record) for record in stores.group)


def test_create_code_reply_is_replaced_in_both_histories(stores):
    chat = SimpleNamespace(id=-100, type="supergroup", title="Group")
    update = _update("/create_code 1 100", chat)
    code = "123e4567-e89b-12d3-a456-426614174000"

    async def run_flow():
        await telegram_history.prepare_update_history(
            update,
            SimpleNamespace(bot=object()),
        )
        await telegram_history._record_bot_message(
            object(),
            _message(f"1. {code} - 100金币", chat=chat, message_id=90, from_id=999),
        )

    asyncio.run(run_flow())

    assert code not in stores.personal[-1]
    assert "[sensitive command output redacted]" in stores.personal[-1]
    assert _group_content(stores.group[-1]) == redaction.SENSITIVE_OUTPUT_PLACEHOLDER


def test_group_history_redacts_credentials_in_any_group_message(monkeypatch):
    written = []

    async def fake_write(record):
        written.append(record)

    monkeypatch.setattr(group_chat_history, "_log_group_message", fake_write)
    chat = SimpleNamespace(id=-100, type="supergroup", title="Group")
    message = _message(
        "see https://example.test/?api_key=SECRETVALUE123&page=1 please",
        chat=chat,
    )

    asyncio.run(group_chat_history.log_group_message(message, -100))

    content = _group_content(written[0])
    assert "SECRETVALUE123" not in content
    assert "page=1" in content


def test_group_history_redacts_caption_before_encoding(monkeypatch):
    written = []

    async def fake_write(record):
        written.append(record)

    monkeypatch.setattr(group_chat_history, "_log_group_message", fake_write)
    chat = SimpleNamespace(id=-100, type="supergroup", title="Group")
    message = _message(None, chat=chat)
    message.caption = f"/charge {SECRET}"
    message.photo = [object()]

    asyncio.run(group_chat_history.log_group_message(message, -100))

    assert written[0][3] == "photo"
    assert SECRET not in _group_content(written[0])
