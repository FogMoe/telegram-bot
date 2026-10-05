"""本地合成负载基准：N 个并发对话的整轮耗时与事件循环延迟（fake provider + fake Telegram，不连网）。

用途：比较执行模型改造前后的行为。同一份脚本可以指向另一份检出的 `src/` 目录运行：

    uv run python scripts/bench_runtime.py --label after
    uv run python scripts/bench_runtime.py --src <另一份检出的 src 目录> --label before

结果是**本地合成负载**的测量：provider 与工具用固定延迟的替身，数据库与 Telegram 全部是替身，
不代表生产环境的吞吐。数字只用来比较两种执行模型在同一假设下的相对行为。
详见 docs/runtime.md 的「基准」。

每个对话走完整的 `handlers._reply_locked` 入口（会话锁、准入、一轮对话的各阶段）：
一次带工具调用的模型回复（先发一段可见文本）-> 一个同步工具 -> 最终回复，再加上假的数据库与 Telegram 延迟。
指标：

- total：从进入入口到整轮结束的耗时（含排队），只统计真正跑完的对话；
- model：模型阶段耗时（旧模型里等线程池槽位的时间也算在这里，因为那里没有显式排队）；
- loop lag：事件循环的最大调度延迟（Windows 上受 15.6ms 的系统时钟粒度影响，只看数量级）；
- busy：被准入拒绝、收到「繁忙」提示的对话数（只有新版本有准入）。
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import inspect
import json
import logging
import os
import statistics
import sys
import time
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--src", type=Path, default=REPO_ROOT / "src", help="要测量的 src 目录（包含 fogmoe_telegram_bot）")
    parser.add_argument("--label", default="run", help="结果标签（before / after）")
    parser.add_argument("--concurrency", default="10,50,200", help="并发对话数，逗号分隔")
    parser.add_argument("--provider-ms", type=float, default=300.0, help="每次模型调用的延迟（毫秒）")
    parser.add_argument("--tool-ms", type=float, default=50.0, help="同步工具的阻塞时间（毫秒）")
    parser.add_argument("--telegram-ms", type=float, default=20.0, help="每次 Telegram 调用的延迟（毫秒）")
    parser.add_argument("--db-ms", type=float, default=5.0, help="每次数据库调用的延迟（毫秒）")
    parser.add_argument("--repeat", type=int, default=3, help="每个并发度重复次数，取各次汇总的中位数")
    parser.add_argument("--max-concurrent", type=int, default=None, help="新版本：CHAT_MAX_CONCURRENT_TURNS")
    parser.add_argument("--max-queued", type=int, default=None, help="新版本：CHAT_MAX_QUEUED_TURNS")
    parser.add_argument("--queue-wait", type=float, default=None, help="新版本：CHAT_QUEUE_MAX_WAIT_SECONDS")
    parser.add_argument("--json", type=Path, default=None, help="把结果写成 JSON")
    return parser.parse_args()


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


class Bench:
    def __init__(self, args: argparse.Namespace) -> None:
        os.environ["BOT_ENV_FILE"] = ""
        # 排在可编辑安装的路径前面，让指定目录里的 fogmoe_telegram_bot 优先被导入。
        sys.path.insert(0, str(args.src.resolve()))
        self.args = args

        import litellm
        from fogmoe_telegram_bot.core import config
        from fogmoe_telegram_bot.features.ai import litellm_client, router, tool_runner
        from fogmoe_telegram_bot.features.conversation import handlers, lifecycle, turn, turn_services

        self.litellm = litellm
        self.config = config
        self.litellm_client = litellm_client
        self.router = router
        self.tool_runner = tool_runner
        self.handlers = handlers
        self.turn = turn
        self.turn_services = turn_services
        self.lifecycle = lifecycle
        self.native_async = inspect.iscoroutinefunction(litellm_client.create_chat_completion)
        self.has_admission = "CHAT_MAX_CONCURRENT_TURNS" in config.AppSettings.model_fields

        values = {
            "AI_CHAT_ORDER": "openai",
            "OPENAI_CHAT_MODEL": "bench-model",
            "OPENAI_API_KEY": "bench-key",
        }
        if self.has_admission:
            if args.max_concurrent is not None:
                values["CHAT_MAX_CONCURRENT_TURNS"] = args.max_concurrent
            if args.max_queued is not None:
                values["CHAT_MAX_QUEUED_TURNS"] = args.max_queued
            if args.queue_wait is not None:
                values["CHAT_QUEUE_MAX_WAIT_SECONDS"] = args.queue_wait
            values["CHAT_MAX_PENDING_PER_USER"] = 3
        config.install_settings(config.AppSettings.from_values(**values))
        self.settings_values = values

        self.install_fakes()

    # -- 替身 ---------------------------------------------------------------------------

    def install_fakes(self) -> None:
        args = self.args
        provider_s = args.provider_ms / 1000
        tool_s = args.tool_ms / 1000
        telegram_s = args.telegram_ms / 1000
        db_s = args.db_ms / 1000

        def response(content, tool_calls=None):
            message = SimpleNamespace(content=content, tool_calls=tool_calls)
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

        tool_call = {
            "id": "call_bench",
            "type": "function",
            "function": {"name": "google_search", "arguments": '{"query": "bench"}'},
        }

        def next_response(messages):
            if any(message.get("role") == "tool" for message in messages):
                return response("查到了，结果在这里。")
            return response("我先查一下。", [tool_call])

        def fake_completion(**kwargs):  # 旧模型：同步调用，占着线程
            time.sleep(provider_s)
            return next_response(kwargs["messages"])

        async def fake_acompletion(**kwargs):  # 新模型：原生 async
            await asyncio.sleep(provider_s)
            return next_response(kwargs["messages"])

        self.litellm.completion = fake_completion
        self.litellm.acompletion = fake_acompletion

        def blocking_tool(**kwargs):
            time.sleep(tool_s)  # 像 requests 一样阻塞
            return {"organic_results": []}

        self.tool_runner.AI_TOOL_HANDLERS["google_search"] = blocking_tool

        from fogmoe_telegram_bot.core import command_cooldown

        async def allow(update):
            return True

        command_cooldown.check_chat_cooldown = allow

        async def db(*args, **kwargs):
            await asyncio.sleep(db_s)

        async def db_none(*args, **kwargs):
            await asyncio.sleep(db_s)
            return None

        from fogmoe_telegram_bot.features.conversation import billing
        from fogmoe_telegram_bot.features.conversation.turn_types import HistoryInsert, UserStateRecord

        async def charge(user_id, messages):
            await asyncio.sleep(db_s)
            return billing.TurnCharge(
                status=billing.TurnChargeStatus.CHARGED,
                total_cost=1,
                newly_charged=1,
                permission=2,
                info="",
                balance_free=10,
                balance_paid=0,
            )

        async def load_user_state(user_id):
            await asyncio.sleep(db_s)
            return UserStateRecord(impression=None, diary_exists=False)

        async def insert(*args, **kwargs) -> HistoryInsert:
            await asyncio.sleep(db_s)
            return (False, None, [])

        async def get_history(conversation_id):
            await asyncio.sleep(db_s)
            return [{"role": "user", "content": "<message>你好</message>"}]

        async def typing(bot, chat_id):
            await asyncio.sleep(telegram_s)

        async def identity(text):
            return text

        base = self.turn_services.default_services()
        services = dataclasses.replace(
            base,
            charge=charge,
            flush_events=db,
            load_user_state=load_user_state,
            insert_records=insert,
            insert_record=insert,
            get_history=get_history,
            schedule_summary=lambda conversation_id: None,
            handle_history_overflow=db,
            arm_idle_followup=db,
            archive_completed_clear=db,
            send_typing=typing,
            send_warning=lambda bot, chat_id, text: typing(bot, chat_id),
            send_archive=db,
            normalize_stickers=identity,
            log_group_message=db,
        )
        self.turn.default_services = lambda: services

        # 记录每一轮的结果（阶段耗时），不改变行为。
        self.results: list = []
        original_run_turn = self.handlers.run_turn

        async def recording_run_turn(request, services=None, settings=None):
            result = await original_run_turn(request, services, settings)
            self.results.append(result)
            return result

        self.handlers.run_turn = recording_run_turn
        self.telegram_s = telegram_s

    # -- 一次运行 --------------------------------------------------------------------------

    def make_item(self, user_id: int, notices: list):
        telegram_s = self.telegram_s

        async def reply_text(text, **kwargs):
            await asyncio.sleep(telegram_s)
            if "繁忙" in text or "busy" in text or "restarting" in text:
                notices.append(user_id)
            return SimpleNamespace(message_id=user_id)

        async def send_message(chat_id, text, **kwargs):
            await asyncio.sleep(telegram_s)
            return SimpleNamespace(message_id=user_id)

        async def send_chat_action(**kwargs):
            await asyncio.sleep(telegram_s)

        message = SimpleNamespace(
            message_id=user_id,
            text="帮我查一下",
            caption=None,
            photo=None,
            sticker=None,
            date=None,
            edit_date=None,
            reply_to_message=None,
            reply_text=reply_text,
        )
        update = SimpleNamespace(
            update_id=user_id,
            message=message,
            edited_message=None,
            effective_chat=SimpleNamespace(id=user_id, type="private", title=None),
            effective_user=SimpleNamespace(
                id=user_id, username=f"u{user_id}", first_name="B", language_code="zh"
            ),
        )
        context = SimpleNamespace(
            bot=SimpleNamespace(send_message=send_message, send_chat_action=send_chat_action)
        )
        return self.handlers.batching._QueuedUpdate(update=update, context=context)

    async def run_once(self, count: int) -> dict:
        self.results.clear()
        self.router._provider_failure_streaks.clear()
        self.router._provider_circuit_open_until.clear()
        notices: list[int] = []
        latencies: dict[int, float] = {}
        loop_lag = {"max": 0.0}
        stop = asyncio.Event()

        async def watch_loop():
            interval = 0.005
            while not stop.is_set():
                before = time.perf_counter()
                await asyncio.sleep(interval)
                lag = time.perf_counter() - before - interval
                loop_lag["max"] = max(loop_lag["max"], lag)

        async def one(user_id: int):
            item = self.make_item(user_id, notices)
            started = time.perf_counter()
            await self.handlers._reply_locked((user_id, user_id), [item])
            latencies[user_id] = time.perf_counter() - started

        watcher = asyncio.create_task(watch_loop())
        wall_start = time.perf_counter()
        await asyncio.gather(*(one(1000 + index) for index in range(count)))
        wall = time.perf_counter() - wall_start
        stop.set()
        await watcher

        from fogmoe_telegram_bot.features.conversation.turn_types import Stage

        # 被准入拒绝的对话（收到「繁忙」提示）几乎立刻返回，不计入整轮耗时的分位数。
        completed = [latency for user_id, latency in latencies.items() if user_id not in notices]
        model_seconds = [result.timings.seconds(Stage.MODEL) for result in self.results]
        queue_seconds = [result.timings.queue_seconds for result in self.results]
        return {
            "conversations": count,
            "completed_turns": len(self.results),
            "busy_notices": len(notices),
            "wall_s": wall,
            "total_p50_s": statistics.median(completed) if completed else 0.0,
            "total_p95_s": percentile(completed, 0.95),
            "total_max_s": max(completed) if completed else 0.0,
            "model_p50_s": statistics.median(model_seconds) if model_seconds else 0.0,
            "model_p95_s": percentile(model_seconds, 0.95),
            "queue_p95_s": percentile(queue_seconds, 0.95),
            "loop_lag_max_ms": loop_lag["max"] * 1000,
            "throughput_per_s": len(self.results) / wall if wall else 0.0,
        }

    async def run(self) -> list[dict]:
        rows = []
        for count in [int(value) for value in self.args.concurrency.split(",")]:
            runs = [await self.run_once(count) for _ in range(self.args.repeat)]
            merged = {key: (statistics.median(run[key] for run in runs) if key != "conversations" else count) for key in runs[0]}
            merged["conversations"] = count
            rows.append(merged)
        return rows


def main() -> None:
    args = parse_args()
    logging.disable(logging.CRITICAL)  # 只看汇总表，不看每一轮的日志
    bench = Bench(args)
    rows = asyncio.run(bench.run())

    header = (
        f"label={args.label} src={args.src} native_async={bench.native_async} "
        f"admission={bench.has_admission} settings={bench.settings_values}\n"
        f"provider={args.provider_ms:g}ms x2 calls, tool={args.tool_ms:g}ms (sync, blocking), "
        f"telegram={args.telegram_ms:g}ms/call, db={args.db_ms:g}ms/call, repeat={args.repeat} (median)"
    )
    print(header)
    columns = [
        ("conv", "conversations", "{:>5.0f}"),
        ("done", "completed_turns", "{:>5.0f}"),
        ("busy", "busy_notices", "{:>5.0f}"),
        ("wall(s)", "wall_s", "{:>8.2f}"),
        ("total p50", "total_p50_s", "{:>10.2f}"),
        ("total p95", "total_p95_s", "{:>10.2f}"),
        ("total max", "total_max_s", "{:>10.2f}"),
        ("model p50", "model_p50_s", "{:>10.2f}"),
        ("model p95", "model_p95_s", "{:>10.2f}"),
        ("queue p95", "queue_p95_s", "{:>10.2f}"),
        ("loop lag max(ms)", "loop_lag_max_ms", "{:>17.0f}"),
        ("turns/s", "throughput_per_s", "{:>8.1f}"),
    ]
    print(" ".join(f"{title:>{len(fmt.format(0.0))}}" for title, _, fmt in columns))
    for row in rows:
        print(" ".join(fmt.format(row[key]) for _, key, fmt in columns))

    if args.json:
        args.json.write_text(
            json.dumps({"label": args.label, "args": {k: str(v) for k, v in vars(args).items()}, "rows": rows}, indent=2),
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
