"""余额与奖池集成测试的共用帮助：造用户、读账本、简单的 Telegram 替身。

与 mysql_support 一样直接 `from economy_support import ...`；访问 core.db 的协程仍然要用
`mysql_support.run()` 运行。
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from mysql_support import execute, fetch, fetch_scalar


def seed_user(
    url: str,
    user_id: int,
    *,
    free: int = 0,
    paid: int = 0,
    plan: str | None = None,
    name: str = "someone",
) -> None:
    if plan is None:
        plan = "paid" if paid > 0 else "free"
    execute(
        url,
        (
            "INSERT INTO `user` (id, name, coins, coins_paid, user_plan) VALUES (%s, %s, %s, %s, %s)",
            (user_id, name, free, paid, plan),
        ),
    )


def user_state(url: str, user_id: int) -> dict[str, Any]:
    rows = fetch(
        url,
        "SELECT coins AS free, coins_paid AS paid, user_plan AS plan FROM `user` WHERE id = %s",
        (user_id,),
    )
    return rows[0]


def total_coins(url: str, user_id: int) -> int:
    state = user_state(url, user_id)
    return state["free"] + state["paid"]


def ledger_rows(url: str, user_id: int | None = None) -> list[dict[str, Any]]:
    sql = (
        "SELECT op_key, user_id, kind, delta_free, delta_paid, balance_free, balance_paid, "
        "reason, ref FROM coin_ledger"
    )
    params: tuple[Any, ...] = ()
    if user_id is not None:
        sql += " WHERE user_id = %s"
        params = (user_id,)
    return fetch(url, sql + " ORDER BY id", params)


def ledger_keys(url: str, user_id: int | None = None) -> list[str]:
    return [row["op_key"] for row in ledger_rows(url, user_id)]


def pool_balance(url: str) -> Decimal:
    return Decimal(str(fetch_scalar(url, "SELECT balance FROM stake_reward_pool WHERE id = 1")))


def pool_rows(url: str) -> list[dict[str, Any]]:
    return fetch(
        url,
        "SELECT op_key, kind, delta, balance_after, reason, ref FROM stake_pool_ledger ORDER BY id",
    )


# ---------------------------------------------------------------------------
# Telegram 替身
# ---------------------------------------------------------------------------


class Recorder:
    """记录每次调用的参数；`fail_times` 指定前几次调用抛异常。"""

    def __init__(self, *, result: Any = None, fail_times: int = 0, error: Exception | None = None):
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self._result = result
        self._fail_times = fail_times
        self._error = error or RuntimeError("injected failure")

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        if len(self.calls) <= self._fail_times:
            raise self._error
        return self._result

    @property
    def texts(self) -> list[str]:
        out = []
        for args, kwargs in self.calls:
            text = kwargs.get("text") or kwargs.get("caption") or (args[0] if args else None)
            if isinstance(text, str):
                out.append(text)
        return out


def make_message(
    *,
    chat_id: int,
    message_id: int,
    user_id: int,
    text: str | None = None,
    reply_text: Recorder | None = None,
) -> Any:
    return SimpleNamespace(
        message_id=message_id,
        text=text,
        chat=SimpleNamespace(id=chat_id, type="private"),
        from_user=SimpleNamespace(id=user_id, username=f"user{user_id}"),
        reply_to_message=None,
        reply_text=reply_text or Recorder(result=SimpleNamespace(message_id=message_id + 1)),
        date=None,
        photo=None,
        sticker=None,
    )


def make_command_update(
    *,
    user_id: int,
    chat_id: int | None = None,
    message_id: int = 1,
    text: str | None = None,
    reply_text: Recorder | None = None,
    update_id: int | None = None,
) -> Any:
    chat_id = user_id if chat_id is None else chat_id
    message = make_message(
        chat_id=chat_id,
        message_id=message_id,
        user_id=user_id,
        text=text,
        reply_text=reply_text,
    )
    return SimpleNamespace(
        update_id=update_id,
        message=message,
        edited_message=None,
        callback_query=None,
        effective_user=SimpleNamespace(id=user_id, username=f"user{user_id}", first_name="U"),
        effective_chat=SimpleNamespace(id=chat_id, type="private", title=None),
    )


def make_callback_update(
    *,
    from_user_id: int,
    data: str,
    chat_id: int | None = None,
    message_id: int = 7,
    query_id: str = "q-1",
) -> tuple[Any, Recorder, Recorder]:
    """返回 (update, answer 记录器, edit_message_text 记录器)。"""
    answer = Recorder()
    edit = Recorder()
    query = SimpleNamespace(
        id=query_id,
        data=data,
        from_user=SimpleNamespace(id=from_user_id, username=f"user{from_user_id}"),
        answer=answer,
        edit_message_text=edit,
        edit_message_caption=Recorder(),
        message=SimpleNamespace(
            message_id=message_id,
            caption="高清原图 点击下方按钮 获取",
            chat=SimpleNamespace(id=chat_id if chat_id is not None else from_user_id),
        ),
    )
    update = SimpleNamespace(
        update_id=None,
        callback_query=query,
        message=None,
        effective_user=query.from_user,
        effective_chat=SimpleNamespace(
            id=chat_id if chat_id is not None else from_user_id, type="private"
        ),
    )
    return update, answer, edit


def make_context(*, send_message: Recorder | None = None, **bot_methods: Any) -> Any:
    bot = SimpleNamespace(
        send_message=send_message or Recorder(),
        send_chat_action=Recorder(),
        **bot_methods,
    )
    return SimpleNamespace(bot=bot, args=[], user_data={})


async def gather_all(*coroutines: Any) -> list[Any]:
    """并发运行并返回每个协程的结果或异常（不会因为某个失败而中断其他）。"""
    return await asyncio.gather(*coroutines, return_exceptions=True)
