"""定时任务与空闲跟进恢复测试共用的替身与数据构造。

场景在单个事件循环里运行（`mysql_support.run`）；循环里用 `a_execute` / `a_fetch` 访问数据库，
循环外用 `mysql_support` 的 `execute` / `fetch`。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from mysql_support import execute, fetch


class SimulatedCrash(BaseException):
    """模拟进程在这一刻消失。

    它不是 Exception：任何 `except Exception` 都拦不住，也不会有收尾写入，
    数据库停在崩溃那一刻的样子。
    """


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send_chat_action(self, **kwargs: Any) -> None:
        return None

    async def send_message(self, *args: Any, **kwargs: Any) -> Any:
        self.sent.append((args, kwargs))
        return SimpleNamespace(message_id=len(self.sent))


class FakeTelegram:
    """Telegram 边界：记录真正“发出去”的内容，可以在发送成功之后崩溃。"""

    def __init__(self) -> None:
        self.delivered: list[str] = []
        self.crash_after_send = False
        self.fail_with: Exception | None = None

    async def send_reply(self, *, bot: Any, chat_id: int, text: str, **kwargs: Any) -> list[Any]:
        if self.fail_with is not None:
            raise self.fail_with
        self.delivered.append(text)
        if self.crash_after_send:
            raise SimulatedCrash("process died after Telegram accepted the message")
        return [SimpleNamespace(message_id=len(self.delivered))]


class FakeAI:
    def __init__(self, reply: str = "drink some water") -> None:
        self.calls = 0
        self.reply = reply
        self.tool_logs: list[dict] = []
        self.hook: Any = None
        self.tool_contexts: list[Any] = []

    async def get_ai_response(
        self,
        messages: Any,
        user_id: int,
        tool_context: Any = None,
        text_fallback_messages: Any = None,
        visible_content_handler: Any = None,
    ) -> tuple[str, list[dict]]:
        self.calls += 1
        self.tool_contexts.append(tool_context)
        if self.hook is not None:
            await self.hook(tool_context)
        return self.reply, [dict(log) for log in self.tool_logs]


# ---------------------------------------------------------------------------
# 事件循环外：构造数据与检查结果
# ---------------------------------------------------------------------------


def seed_user(url: str, user_id: int, *, coins: int = 10, coins_paid: int = 0) -> None:
    execute(
        url,
        (
            "INSERT INTO `user` (id, name, coins, coins_paid) VALUES (%s, %s, %s, %s)",
            (user_id, f"user{user_id}", coins, coins_paid),
        ),
    )


def seed_schedule(
    url: str,
    user_id: int,
    *,
    unit: str = "none",
    interval: int = 1,
    minutes_overdue: int = 5,
) -> int:
    execute(
        url,
        (
            "INSERT INTO ai_schedules (user_id, run_at, recurrence_unit, recurrence_interval, "
            "trigger_reason, prompt) VALUES (%s, UTC_TIMESTAMP() - INTERVAL %s MINUTE, %s, %s, "
            "'reminder', 'remind me to drink water')",
            (user_id, minutes_overdue, unit, interval),
        ),
    )
    return int(fetch(url, "SELECT MAX(id) AS id FROM ai_schedules")[0]["id"])


def schedule_row(url: str, schedule_id: int) -> dict[str, Any]:
    return fetch(url, "SELECT * FROM ai_schedules WHERE id = %s", (schedule_id,))[0]


def seed_history(url: str, user_id: int) -> None:
    """空闲跟进需要有可回顾的对话。"""
    from mysql_support import run

    from core import chat_records

    run(chat_records.insert_chat_record(user_id, "user", "hello there"))
    run(chat_records.insert_chat_record(user_id, "assistant", "hi, how can I help?"))


def seed_idle_followup(url: str, user_id: int, *, version: int = 1, minutes_overdue: int = 5) -> None:
    execute(
        url,
        (
            "INSERT INTO ai_idle_followups (user_id, last_activity_at, last_turn_at, next_run_at, "
            "typical_interval_seconds, recent_intervals, activity_version, status) VALUES "
            "(%s, UTC_TIMESTAMP() - INTERVAL 1 HOUR, UTC_TIMESTAMP() - INTERVAL 1 HOUR, "
            "UTC_TIMESTAMP() - INTERVAL %s MINUTE, 600, '[]', %s, 'armed')",
            (user_id, minutes_overdue, version),
        ),
    )


def idle_row(url: str, user_id: int) -> dict[str, Any]:
    return fetch(url, "SELECT * FROM ai_idle_followups WHERE user_id = %s", (user_id,))[0]


def attempts(url: str, job_type: str, job_id: int) -> list[dict[str, Any]]:
    return fetch(
        url,
        "SELECT * FROM ai_job_attempts WHERE job_type = %s AND job_id = %s ORDER BY id",
        (job_type, job_id),
    )


def expire_lease(url: str, table: str, key_column: str, key: int) -> None:
    execute(
        url,
        (
            f"UPDATE {table} SET claim_until = UTC_TIMESTAMP() - INTERVAL 5 SECOND "
            f"WHERE {key_column} = %s AND status = 'executing'",
            (key,),
        ),
    )


def history_messages(url: str, user_id: int) -> list[dict]:
    import json

    rows = fetch(url, "SELECT messages FROM chat_records WHERE conversation_id = %s", (user_id,))
    return json.loads(rows[0]["messages"]) if rows else []


# ---------------------------------------------------------------------------
# 事件循环内：直接用应用的引擎
# ---------------------------------------------------------------------------


async def a_execute(sql: str, params: tuple = ()) -> int:
    from core import sql as core_sql

    return await core_sql.execute(sql, params)


async def a_fetch(sql: str, params: tuple = ()) -> list[Any]:
    from core import sql as core_sql

    return list(await core_sql.fetch_all(sql, params))
