import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from telegram.constants import ChatMemberStatus

from fogmoe_telegram_bot.features.moderation import member_verify
from fogmoe_telegram_bot.features.moderation.member_verify import is_own_verify_button

CHAT_ID = -100
USER_ID = 42


def test_own_button_is_accepted():
    assert is_own_verify_button("verify_42", USER_ID)


def test_button_from_before_upgrade_is_accepted():
    assert is_own_verify_button("verify_42_0123abcd", USER_ID)


def test_someone_elses_button_is_rejected():
    assert not is_own_verify_button("verify_7", USER_ID)


def test_malformed_callback_data_is_rejected():
    assert not is_own_verify_button("verify", USER_ID)


class _FakeTasks:
    """verification_tasks 的内存替身：(user_id, chat_id) -> (message_id, expire_time)。"""

    def __init__(self):
        self.rows = {}

    async def save(self, user_id, chat_id, message_id, expire_time):
        self.rows[(user_id, chat_id)] = (message_id, expire_time)

    async def claim(self, user_id, chat_id, message_id):
        row = self.rows.get((user_id, chat_id))
        if row is None or row[0] != message_id:
            return False
        del self.rows[(user_id, chat_id)]
        return True

    async def find(self, user_id, chat_id):
        row = self.rows.get((user_id, chat_id))
        return row[0] if row else None

    async def due(self, now):
        return [
            (user_id, chat_id, message_id)
            for (user_id, chat_id), (message_id, expire_time) in self.rows.items()
            if expire_time <= now
        ]


@pytest.fixture
def tasks(monkeypatch):
    fake = _FakeTasks()
    monkeypatch.setattr(member_verify, "save_verification_task", fake.save)
    monkeypatch.setattr(member_verify, "claim_verification_task", fake.claim)
    monkeypatch.setattr(member_verify, "find_verification_message", fake.find)
    monkeypatch.setattr(member_verify, "due_verification_tasks", fake.due)
    return fake


class _FakeBot:
    def __init__(self, status=ChatMemberStatus.RESTRICTED, can_send_messages=False):
        self.member = SimpleNamespace(status=status, can_send_messages=can_send_messages)
        self.restricted = []
        self.kicked = []
        self.edited = []
        self.fail_restrict = False

    async def get_chat_member(self, chat_id, user_id):
        return self.member

    async def restrict_chat_member(self, chat_id, user_id, permissions):
        if self.fail_restrict:
            raise RuntimeError("network down")
        self.restricted.append(user_id)

    async def ban_chat_member(self, chat_id, user_id):
        self.kicked.append(user_id)

    async def unban_chat_member(self, chat_id, user_id):
        pass

    async def edit_message_text(self, chat_id, message_id, text):
        self.edited.append(message_id)

    async def send_message(self, chat_id, text):
        pass


class _FakeQuery:
    def __init__(self, data, message_id):
        self.data = data
        self.from_user = SimpleNamespace(id=USER_ID)
        self.message = SimpleNamespace(message_id=message_id)
        self.answers = []
        self.deleted = False

    async def answer(self, text, show_alert=False):
        self.answers.append(text)

    async def edit_message_text(self, text):
        pass

    async def delete_message(self):
        self.deleted = True


def _later():
    return datetime.now() + timedelta(minutes=5)


def _click(bot, message_id, data="verify_42"):
    query = _FakeQuery(data, message_id)
    update = SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=CHAT_ID))
    asyncio.run(member_verify.verify_callback(update, SimpleNamespace(bot=bot)))
    return query


def test_click_on_current_welcome_message_unmutes(tasks):
    tasks.rows[(USER_ID, CHAT_ID)] = (2, _later())
    bot = _FakeBot()

    _click(bot, message_id=2)

    assert bot.restricted == [USER_ID]
    assert tasks.rows == {}


def test_click_on_old_welcome_message_after_rejoin_is_stale(tasks):
    # 重新入群后记录指向新的欢迎消息 2，旧消息 1 上的按钮不能通过验证
    tasks.rows[(USER_ID, CHAT_ID)] = (2, _later())
    bot = _FakeBot()

    query = _click(bot, message_id=1)

    assert bot.restricted == []
    assert tasks.rows[(USER_ID, CHAT_ID)][0] == 2
    assert query.deleted


def test_failed_unmute_keeps_the_round_open(tasks):
    tasks.rows[(USER_ID, CHAT_ID)] = (2, _later())
    bot = _FakeBot()
    bot.fail_restrict = True

    _click(bot, message_id=2)

    assert tasks.rows[(USER_ID, CHAT_ID)][0] == 2


def test_timeout_of_old_round_after_rejoin_does_nothing(tasks):
    tasks.rows[(USER_ID, CHAT_ID)] = (2, _later())
    bot = _FakeBot()

    asyncio.run(member_verify.expire_verification(bot, CHAT_ID, USER_ID, 1))

    assert bot.kicked == []
    assert tasks.rows[(USER_ID, CHAT_ID)][0] == 2


def test_timeout_kicks_member_still_muted(tasks):
    tasks.rows[(USER_ID, CHAT_ID)] = (2, _later())
    bot = _FakeBot()

    asyncio.run(member_verify.expire_verification(bot, CHAT_ID, USER_ID, 2))

    assert bot.kicked == [USER_ID]
    assert bot.edited == [2]
    assert tasks.rows == {}


def test_timeout_spares_member_an_admin_already_unmuted(tasks):
    tasks.rows[(USER_ID, CHAT_ID)] = (2, _later())
    bot = _FakeBot(status=ChatMemberStatus.MEMBER)

    asyncio.run(member_verify.expire_verification(bot, CHAT_ID, USER_ID, 2))

    assert bot.kicked == []
    assert tasks.rows == {}


def test_recovery_after_restart_handles_only_expired_rounds(tasks):
    # 重启后内存里的定时器都没了，恢复任务按表里的到期时间处理
    tasks.rows[(USER_ID, CHAT_ID)] = (2, datetime.now() - timedelta(seconds=1))
    tasks.rows[(7, CHAT_ID)] = (3, _later())
    bot = _FakeBot()

    asyncio.run(member_verify.recover_verification_tasks(SimpleNamespace(bot=bot)))

    assert bot.kicked == [USER_ID]
    assert list(tasks.rows) == [(7, CHAT_ID)]


def test_leaving_before_verifying_closes_the_round(tasks):
    tasks.rows[(USER_ID, CHAT_ID)] = (2, _later())
    bot = _FakeBot()

    async def get_me():
        return SimpleNamespace(id=1)

    bot.get_me = get_me
    left = SimpleNamespace(id=USER_ID, full_name="someone")
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=CHAT_ID),
        message=SimpleNamespace(left_chat_member=left),
    )

    asyncio.run(member_verify.handle_member_left(update, SimpleNamespace(bot=bot)))

    assert tasks.rows == {}
    assert bot.edited == [2]
