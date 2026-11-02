import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from telegram.constants import ChatMemberStatus

from fogmoe_telegram_bot.features.moderation import admin_ban_views, member_verify
from fogmoe_telegram_bot.features.moderation.member_verify import is_own_verify_button
from fogmoe_telegram_bot.features.moderation.operations import join_messages
from fogmoe_telegram_bot.features.moderation.operations import member_verify as verification
from fogmoe_telegram_bot.features.moderation.repositories.verification import VerificationTask

CHAT_ID = -100
USER_ID = 42
BOT_ID = 1


@pytest.fixture(autouse=True)
def _fresh_joins(monkeypatch):
    monkeypatch.setattr(join_messages, "JOINS", join_messages.JoinMessages())


def test_own_button_is_accepted():
    assert is_own_verify_button("verify_42", USER_ID)


def test_button_from_before_upgrade_is_accepted():
    assert is_own_verify_button("verify_42_0123abcd", USER_ID)


def test_someone_elses_button_is_rejected():
    assert not is_own_verify_button("verify_7", USER_ID)


def test_malformed_callback_data_is_rejected():
    assert not is_own_verify_button("verify", USER_ID)


class _FakeTasks:
    """验证记录的内存替身：(user_id, chat_id) -> (message_id, expire_time)。"""

    def __init__(self):
        self.rows = {}

    async def open(self, user_id, chat_id, message_id):
        self.rows[(user_id, chat_id)] = (message_id, _later())

    async def claim(self, user_id, chat_id, message_id):
        row = self.rows.get((user_id, chat_id))
        if row is None or row[0] != message_id:
            return False
        del self.rows[(user_id, chat_id)]
        return True

    async def find(self, user_id, chat_id):
        row = self.rows.get((user_id, chat_id))
        return row[0] if row else None

    async def due(self):
        now = datetime.now()
        return [
            VerificationTask(user_id, chat_id, message_id)
            for (user_id, chat_id), (message_id, expire_time) in self.rows.items()
            if expire_time <= now
        ]


@pytest.fixture
def tasks(monkeypatch):
    fake = _FakeTasks()
    monkeypatch.setattr(verification, "open_round", fake.open)
    monkeypatch.setattr(verification, "claim_round", fake.claim)
    monkeypatch.setattr(verification, "find_round_message", fake.find)
    monkeypatch.setattr(verification, "due_rounds", fake.due)
    return fake


class _FakeBot:
    def __init__(self, status=ChatMemberStatus.RESTRICTED, can_send_messages=False):
        self.member = SimpleNamespace(status=status, can_send_messages=can_send_messages)
        self.restricted = []
        self.kicked = []
        self.edited = []
        self.edited_texts = []
        self.deleted = []
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
        self.edited_texts.append(text)

    async def send_message(self, chat_id, text):
        pass

    async def delete_message(self, chat_id, message_id):
        self.deleted.append(message_id)

    async def get_me(self):
        return SimpleNamespace(id=BOT_ID)


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


class _Recorder:
    def __init__(self, result=None):
        self.calls = []
        self.result = result

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


def _left(bot, *, removed_by):
    left = SimpleNamespace(id=USER_ID, full_name="加V领福利")
    service_message = SimpleNamespace(left_chat_member=left, from_user=removed_by, delete=_Recorder())
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=CHAT_ID), message=service_message)
    asyncio.run(member_verify.handle_member_left(update, SimpleNamespace(bot=bot)))
    return service_message


def test_leaving_before_verifying_closes_the_round_and_cleans_up(tasks):
    tasks.rows[(USER_ID, CHAT_ID)] = (2, _later())
    join_messages.JOINS.record(CHAT_ID, USER_ID, 1)
    bot = _FakeBot()

    service_message = _left(bot, removed_by=SimpleNamespace(id=USER_ID))

    assert tasks.rows == {}
    assert bot.edited == [2] and bot.edited_texts == ["新成员在验证前离开了群组。"]
    assert bot.deleted == [1]  # 加群消息
    assert service_message.delete.calls == [((), {})]  # 「X 离开了群组」


def test_removal_by_the_bot_deletes_the_service_message(tasks):
    bot = _FakeBot()

    service_message = _left(bot, removed_by=SimpleNamespace(id=BOT_ID))

    assert service_message.delete.calls == [((), {})]


def test_a_verified_member_leaving_on_their_own_is_left_alone(tasks):
    bot = _FakeBot()

    service_message = _left(bot, removed_by=SimpleNamespace(id=USER_ID))

    assert service_message.delete.calls == [] and bot.edited == []


def test_a_timed_out_member_also_loses_the_join_message(tasks):
    tasks.rows[(USER_ID, CHAT_ID)] = (2, _later())
    join_messages.JOINS.record(CHAT_ID, USER_ID, 1)
    bot = _FakeBot()

    asyncio.run(member_verify.expire_verification(bot, CHAT_ID, USER_ID, 2))

    assert bot.kicked == [USER_ID] and bot.deleted == [1]


def _join(monkeypatch, *, enabled):
    async def is_enabled(chat_id):
        return enabled

    monkeypatch.setattr(verification, "is_enabled", is_enabled)
    replies = []

    async def reply_text(text, **kwargs):
        replies.append((text, kwargs))
        return SimpleNamespace(message_id=2)

    new_member = SimpleNamespace(id=USER_ID, is_bot=False, full_name="加V领福利")
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=CHAT_ID),
        message=SimpleNamespace(message_id=1, new_chat_members=[new_member], reply_text=reply_text),
    )
    context = SimpleNamespace(
        bot=_FakeBot(), chat_data={}, job_queue=SimpleNamespace(run_once=lambda *args, **kwargs: None)
    )
    asyncio.run(member_verify.new_member_handler(update, context))
    return replies


def test_the_welcome_hides_the_name_and_offers_admins_a_ban_button(tasks, monkeypatch):
    replies = _join(monkeypatch, enabled=True)

    (text, kwargs), = replies
    assert "加V领福利" not in text and f'<a href="tg://user?id={USER_ID}">新成员</a>' in text
    verify, ban = kwargs["reply_markup"].inline_keyboard[0]
    assert verify.callback_data == f"verify_{USER_ID}"
    assert ban.callback_data == admin_ban_views.ban_data(CHAT_ID, USER_ID)
    assert join_messages.JOINS.pop(CHAT_ID, USER_ID) == 1


def test_join_messages_are_remembered_even_without_verification(tasks, monkeypatch):
    assert _join(monkeypatch, enabled=False) == []
    assert join_messages.JOINS.pop(CHAT_ID, USER_ID) == 1
