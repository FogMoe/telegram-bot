"""游戏集成测试的共用帮助：Telegram 替身、job 队列替身、直接改写截止时间。

与 economy_support 一样直接 `from game_support import ...`；访问 core.db 的协程仍然要用
`mysql_support.run()` 运行。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from economy_support import Recorder
from mysql_support import execute


class MessageSender:
    """`bot.send_message` 的替身：按顺序返回递增的 message_id，可指定前几次失败。"""

    def __init__(self, *, first_id: int = 1000, fail_times: int = 0) -> None:
        self.calls: list[dict[str, Any]] = []
        self._next_id = first_id
        self._fail_times = fail_times

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if len(self.calls) <= self._fail_times:
            raise RuntimeError("injected send failure")
        message = SimpleNamespace(message_id=self._next_id)
        self._next_id += 1
        return message


class FakeJobQueue:
    def __init__(self) -> None:
        self.jobs: list[SimpleNamespace] = []

    def run_once(self, callback: Any, when: float, data: Any = None, name: str | None = None) -> None:
        self.jobs.append(SimpleNamespace(callback=callback, when=when, data=data, name=name))

    def run_repeating(
        self, callback: Any, interval: float, first: float | None = None, **kwargs: Any
    ) -> None:
        self.jobs.append(
            SimpleNamespace(callback=callback, interval=interval, first=first, name=None)
        )


def make_game_bot(
    *, edit: Recorder | None = None, send: MessageSender | None = None
) -> SimpleNamespace:
    return SimpleNamespace(
        edit_message_text=edit or Recorder(),
        send_message=send or MessageSender(),
    )


def make_game_context(
    *, edit: Recorder | None = None, send: MessageSender | None = None
) -> Any:
    return SimpleNamespace(
        bot=make_game_bot(edit=edit, send=send),
        job_queue=FakeJobQueue(),
        args=[],
        user_data={},
    )


def make_job_context(bot: Any, data: Any = None) -> Any:
    """给 job 回调用的 context：`context.bot` 与 `context.job.data`。"""
    return SimpleNamespace(bot=bot, job=SimpleNamespace(data=data), job_queue=FakeJobQueue())


def set_permission(url: str, user_id: int, permission: int = 1) -> None:
    execute(url, ("UPDATE `user` SET permission = %s WHERE id = %s", (permission, user_id)))


def seed_character(
    url: str,
    user_id: int,
    *,
    hp: int = 10,
    max_hp: int = 10,
    experience: int = 0,
    atk: int = 2,
    defense: int = 1,
) -> None:
    execute(
        url,
        (
            "INSERT INTO rpg_characters (user_id, level, hp, max_hp, atk, matk, def, experience, "
            "allow_battle) VALUES (%s, 1, %s, %s, %s, 0, %s, %s, TRUE)",
            (user_id, hp, max_hp, atk, defense, experience),
        ),
    )
