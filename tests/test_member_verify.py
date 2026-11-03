import asyncio
import random
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from telegram.constants import ChatMemberStatus
from telegram.error import BadRequest

from fogmoe_telegram_bot.features.moderation import admin_ban_views, member_verify
from fogmoe_telegram_bot.features.moderation import member_verify_views as views
from fogmoe_telegram_bot.features.moderation.operations import join_messages, verify_game
from fogmoe_telegram_bot.features.moderation.operations import member_verify as verification
from fogmoe_telegram_bot.features.moderation.repositories.verification import VerificationTask

CHAT_ID = -100
USER_ID = 42
BOT_ID = 1


@pytest.fixture(autouse=True)
def _fresh_joins(monkeypatch):
    monkeypatch.setattr(join_messages, "JOINS", join_messages.JoinMessages())


@pytest.fixture(autouse=True)
def _fresh_games(monkeypatch):
    monkeypatch.setattr(verify_game, "GAMES", verify_game.VerifyGames(rng=random.Random(0)))


def test_own_buttons_are_understood():
    assert views.parse_button("verify_42", USER_ID) == views.VerifyButton("start")
    assert views.parse_button(views.hint_data(USER_ID), USER_ID) == views.VerifyButton("hint")
    assert views.parse_button(views.pick_data(USER_ID, 3, 8), USER_ID) == views.VerifyButton("pick", 3, 8)


def test_button_from_before_upgrade_starts_the_game():
    assert views.parse_button("verify_42_0123abcd", USER_ID) == views.VerifyButton("start")


def test_someone_elses_button_is_rejected():
    assert views.parse_button("verify_7", USER_ID) is None
    assert views.parse_button(views.pick_data(7, 1, 0), USER_ID) is None


def test_malformed_callback_data_is_rejected():
    assert views.parse_button("verify", USER_ID) is None
    assert views.parse_button("verify_42_pick_x_1", USER_ID) is None


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
        self.parse_modes = []
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

    async def edit_message_text(self, chat_id, message_id, text, parse_mode=None):
        self.edited.append(message_id)
        self.edited_texts.append(text)
        self.parse_modes.append(parse_mode)

    async def send_message(self, chat_id, text):
        pass

    async def delete_message(self, chat_id, message_id):
        self.deleted.append(message_id)

    async def get_me(self):
        return SimpleNamespace(id=BOT_ID)


class _FakeJobQueue:
    """只记下定时任务；`run_all` 当作都已经到点，一次全部执行。"""

    def __init__(self):
        self.jobs = []

    def run_once(self, callback, when, data=None):
        self.jobs.append((callback, when, data))

    def run_all(self, bot):
        jobs, self.jobs = self.jobs, []
        for callback, _when, data in jobs:
            asyncio.run(callback(SimpleNamespace(bot=bot, job=SimpleNamespace(data=data))))


class _FakeQuery:
    def __init__(self, data, message_id, *, fail_answer=False, fail_edit=False):
        self.data = data
        self.fail_answer = fail_answer
        self.fail_edit = fail_edit
        self.from_user = SimpleNamespace(id=USER_ID)
        self.message = SimpleNamespace(message_id=message_id)
        self.answers = []
        self.edits = []
        self.deleted = False

    async def answer(self, text, show_alert=False):
        if self.fail_answer:
            raise RuntimeError("query is too old")
        self.answers.append(text)

    async def edit_message_text(self, text, **kwargs):
        if self.fail_edit:
            raise RuntimeError("network down")
        self.edits.append((text, kwargs))

    async def delete_message(self):
        self.deleted = True


def _later():
    return datetime.now() + timedelta(minutes=5)


def _click(bot, message_id, data="verify_42", *, jobs=None, **failures):
    query = _FakeQuery(data, message_id, **failures)
    update = SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=CHAT_ID))
    context = SimpleNamespace(bot=bot, job_queue=jobs or _FakeJobQueue())
    asyncio.run(member_verify.verify_callback(update, context))
    return query


def _play(bot, message_id=2, *, correct=True, **options):
    """点这一局当前这道题的格子：correct 时点目标，否则点旁边一格。"""
    game = verify_game.GAMES.get(CHAT_ID, USER_ID, message_id)
    cell = game.target if correct else (game.target + 1) % verify_game.GRID_SIZE
    return _click(bot, message_id, views.pick_data(USER_ID, game.question, cell), **options)


def test_starting_shows_the_grid_and_puts_the_target_only_in_the_popup(tasks):
    tasks.rows[(USER_ID, CHAT_ID)] = (2, _later())
    bot = _FakeBot()

    query = _click(bot, message_id=2)

    game = verify_game.GAMES.get(CHAT_ID, USER_ID, 2)
    (text, kwargs), = query.edits
    rows = kwargs["reply_markup"].inline_keyboard
    assert [button.text for row in rows[:-1] for button in row] == list(game.icons)
    assert game.target_icon not in text
    assert query.answers == [views.prompt(game)] and game.target_icon in query.answers[0]
    assert bot.restricted == [] and (USER_ID, CHAT_ID) in tasks.rows


def test_enough_correct_answers_in_a_row_unmute(tasks):
    tasks.rows[(USER_ID, CHAT_ID)] = (2, _later())
    bot = _FakeBot()
    _click(bot, message_id=2)

    for _ in range(verify_game.ROUNDS_TO_PASS - 1):
        _play(bot)
        assert bot.restricted == []
    query = _play(bot)

    assert bot.restricted == [USER_ID]
    assert query.answers == [views.PASSED_ALERT]
    assert tasks.rows == {} and verify_game.GAMES.get(CHAT_ID, USER_ID, 2) is None


def test_too_many_mistakes_remove_the_member(tasks):
    tasks.rows[(USER_ID, CHAT_ID)] = (2, _later())
    join_messages.JOINS.record(CHAT_ID, USER_ID, 1)
    bot, jobs = _FakeBot(), _FakeJobQueue()
    _click(bot, message_id=2)

    for _ in range(verify_game.MAX_MISTAKES):
        query = _play(bot, correct=False, jobs=jobs)

    assert bot.restricted == [] and bot.kicked == [USER_ID]
    assert query.answers == [views.FAILED_ALERT]
    assert bot.edited_texts == [views.failed_text(USER_ID)] and bot.deleted == [1]
    assert tasks.rows == {}

    jobs.run_all(bot)  # 移出说明过一会儿也删掉

    assert bot.deleted == [1, 2]


def test_a_failed_popup_still_removes_the_member(tasks):
    # 第三次答错时弹框发不出去：这一轮已经认领，超时不会再处理，必须照常移出
    tasks.rows[(USER_ID, CHAT_ID)] = (2, _later())
    bot = _FakeBot()
    _click(bot, message_id=2)
    for _ in range(verify_game.MAX_MISTAKES - 1):
        _play(bot, correct=False)

    _play(bot, correct=False, fail_answer=True)

    assert bot.kicked == [USER_ID] and tasks.rows == {}


def test_a_failed_success_edit_does_not_reopen_the_round(tasks):
    # 已经解除禁言，只是没能把欢迎消息改成「验证通过」：不放回这一轮，也不重新出题
    tasks.rows[(USER_ID, CHAT_ID)] = (2, _later())
    bot = _FakeBot()
    _click(bot, message_id=2)
    for _ in range(verify_game.ROUNDS_TO_PASS - 1):
        _play(bot)

    query = _play(bot, fail_edit=True)

    assert bot.restricted == [USER_ID] and tasks.rows == {}
    assert query.answers == [views.PASSED_ALERT]


def test_after_a_restart_the_old_grid_starts_a_new_game(tasks):
    # 重启丢了内存里的局：记录还在时，点旧九宫格上的格子重新开一局，不算对错
    tasks.rows[(USER_ID, CHAT_ID)] = (2, _later())
    bot = _FakeBot()

    query = _click(bot, 2, views.pick_data(USER_ID, 3, 0))

    game = verify_game.GAMES.get(CHAT_ID, USER_ID, 2)
    assert game.mistakes == 0 and game.streak == 0
    assert query.answers == [views.prompt(game)] and len(query.edits) == 1


def test_click_on_old_welcome_message_after_rejoin_is_stale(tasks):
    # 重新入群后记录指向新的欢迎消息 2，旧消息 1 上的按钮不能通过验证
    tasks.rows[(USER_ID, CHAT_ID)] = (2, _later())
    bot = _FakeBot()

    query = _click(bot, message_id=1)

    assert bot.restricted == []
    assert tasks.rows[(USER_ID, CHAT_ID)][0] == 2
    assert query.deleted


def test_failed_unmute_keeps_the_round_open_and_the_game_won(tasks):
    tasks.rows[(USER_ID, CHAT_ID)] = (2, _later())
    bot = _FakeBot()
    bot.fail_restrict = True
    _click(bot, message_id=2)
    for _ in range(verify_game.ROUNDS_TO_PASS):
        _play(bot)

    assert tasks.rows[(USER_ID, CHAT_ID)][0] == 2

    # 再点任意一个按钮就再试一次解除禁言，不用重玩
    bot.fail_restrict = False
    _click(bot, 2, views.hint_data(USER_ID))

    assert bot.restricted == [USER_ID] and tasks.rows == {}


def _expire(bot, message_id, jobs=None):
    asyncio.run(member_verify.expire_verification(bot, jobs or _FakeJobQueue(), CHAT_ID, USER_ID, message_id))


def test_timeout_of_old_round_after_rejoin_does_nothing(tasks):
    tasks.rows[(USER_ID, CHAT_ID)] = (2, _later())
    bot = _FakeBot()

    _expire(bot, 1)

    assert bot.kicked == []
    assert tasks.rows[(USER_ID, CHAT_ID)][0] == 2


def test_timeout_kicks_member_still_muted(tasks):
    tasks.rows[(USER_ID, CHAT_ID)] = (2, _later())
    bot = _FakeBot()

    _expire(bot, 2)

    assert bot.kicked == [USER_ID]
    assert bot.edited == [2]
    assert tasks.rows == {}


def test_the_removal_notice_hides_the_name_and_is_deleted_later(tasks):
    # 被移出的人看不到这条说明：不写「您」，也不显示名字；留一会儿再删掉
    tasks.rows[(USER_ID, CHAT_ID)] = (2, _later())
    bot, jobs = _FakeBot(), _FakeJobQueue()

    _expire(bot, 2, jobs)

    (text,) = bot.edited_texts
    assert text == views.timeout_text(USER_ID) and bot.parse_modes == ["HTML"]
    assert "您" not in text and f'<a href="tg://user?id={USER_ID}">新成员</a>' in text
    assert bot.deleted == []
    assert [when for _, when, _ in jobs.jobs] == [member_verify.REMOVAL_NOTICE_SECONDS]

    jobs.run_all(bot)

    assert bot.deleted == [2]


def test_a_notice_an_admin_already_deleted_is_left_alone(tasks):
    class _GoneBot(_FakeBot):
        async def delete_message(self, chat_id, message_id):
            raise BadRequest("Message to delete not found")

    tasks.rows[(USER_ID, CHAT_ID)] = (2, _later())
    bot, jobs = _GoneBot(), _FakeJobQueue()
    _expire(bot, 2, jobs)

    jobs.run_all(bot)  # 不抛出


def test_timeout_spares_member_an_admin_already_unmuted(tasks):
    tasks.rows[(USER_ID, CHAT_ID)] = (2, _later())
    bot = _FakeBot(status=ChatMemberStatus.MEMBER)

    _expire(bot, 2)

    assert bot.kicked == []
    assert tasks.rows == {}


def test_recovery_after_restart_handles_only_expired_rounds(tasks):
    # 重启后内存里的定时器都没了，恢复任务按表里的到期时间处理
    tasks.rows[(USER_ID, CHAT_ID)] = (2, datetime.now() - timedelta(seconds=1))
    tasks.rows[(7, CHAT_ID)] = (3, _later())
    bot, jobs = _FakeBot(), _FakeJobQueue()

    asyncio.run(member_verify.recover_verification_tasks(SimpleNamespace(bot=bot, job_queue=jobs)))

    assert bot.kicked == [USER_ID]
    assert list(tasks.rows) == [(7, CHAT_ID)]
    assert [data for _, _, data in jobs.jobs] == [(CHAT_ID, 2)]


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

    _expire(bot, 2)

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
