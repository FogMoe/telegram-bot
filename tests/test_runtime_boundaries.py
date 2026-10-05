"""运行时边界：事件循环里不做阻塞调用，线程只出现在有界适配器里，`run_sync` 只留在同步边界。

这些是针对源码的结构性检查：新增代码违反边界时直接失败，清单（ALLOWED_*）记录了仍然存在的例外与理由，
见 docs/runtime.md 的「线程适配器清单」。
"""

import ast
import asyncio
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from fogmoe_telegram_bot.core import blocking, config
from fogmoe_telegram_bot.features.ai import sticker_sender
from fogmoe_telegram_bot.features.crypto import biance_api, crypto_predict, monitoring

PACKAGE_DIR = config.RESOURCES_DIR.parent


def _parsed_modules():
    for path in sorted(PACKAGE_DIR.rglob("*.py")):
        yield path.relative_to(PACKAGE_DIR).as_posix(), ast.parse(path.read_text(encoding="utf-8"))


def _dotted(node: ast.AST) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


class _AsyncBodyCalls(ast.NodeVisitor):
    """收集 `async def` 函数体里直接发生的调用（不含嵌套的同步函数，它们可能在线程里运行）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int, str]] = []
        self._async_depth = 0
        self._current = ""

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        previous = self._current
        self._current = node.name
        self._async_depth += 1
        self.generic_visit(node)
        self._async_depth -= 1
        self._current = previous

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        saved_depth, self._async_depth = self._async_depth, 0
        self.generic_visit(node)
        self._async_depth = saved_depth

    def visit_Call(self, node: ast.Call) -> None:
        if self._async_depth:
            self.calls.append((_dotted(node.func), node.lineno, self._current))
        self.generic_visit(node)


BLOCKING_CALLS = {
    "time.sleep",
    "requests.get",
    "requests.post",
    "requests.put",
    "requests.delete",
    "requests.request",
    "requests.Session",
    "urllib.request.urlopen",
    "subprocess.run",
    "subprocess.check_output",
    "litellm.completion",
    "UMFutures",
    "Sandbox.create",
}
BLOCKING_METHODS = {"mark_price", "mark_price_klines"}

# 仍然存在的例外：（文件，函数）。目前为空；新增必须在这里写明理由。
ALLOWED_BLOCKING_IN_ASYNC: set[tuple[str, str]] = set()


@pytest.mark.slow
def test_no_async_function_makes_a_blocking_network_call_directly():
    offenders = []
    for relative, tree in _parsed_modules():
        visitor = _AsyncBodyCalls()
        visitor.visit(tree)
        for name, line, function in visitor.calls:
            blocked = name in BLOCKING_CALLS or name.rsplit(".", 1)[-1] in BLOCKING_METHODS
            if blocked and (relative, function) not in ALLOWED_BLOCKING_IN_ASYNC:
                offenders.append(f"{relative}:{line} {function}() calls {name}")

    assert offenders == []


# 允许出现 ThreadPoolExecutor 的文件：线程只应该出现在有界适配器里。
ALLOWED_THREAD_POOLS = {
    "core/blocking.py",  # 有界适配器本身
    # 没有任何调用者的遗留对象，属于 games/，不在本次范围内；见 docs/runtime.md。
    "features/games/rpg/utils.py",
}


@pytest.mark.slow
def test_thread_pools_only_live_in_the_bounded_adapter():
    offenders = []
    for relative, tree in _parsed_modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _dotted(node.func).endswith("ThreadPoolExecutor"):
                if relative not in ALLOWED_THREAD_POOLS:
                    offenders.append(f"{relative}:{node.lineno}")

    assert offenders == []


# `asyncio.to_thread` / `run_in_executor` 走事件循环的默认线程池（上限随 CPU 数，不可配置）。
# 只剩一处：Argon2id 口令哈希，纯 CPU、不碰网络，属于 economy/，不在本次范围内。
ALLOWED_DEFAULT_EXECUTOR = {"features/economy/operations/web_password.py"}


@pytest.mark.slow
def test_the_default_executor_is_not_used_for_network_or_tool_work():
    offenders = []
    for relative, tree in _parsed_modules():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _dotted(node.func)
            if name.endswith(("asyncio.to_thread", "run_in_executor")) and relative not in {
                "core/blocking.py",
                *ALLOWED_DEFAULT_EXECUTOR,
            }:
                offenders.append(f"{relative}:{node.lineno} {name}")

    assert offenders == []


# `run_sync` 把协程投递回主事件循环并阻塞等待：只允许存在于明确的同步边界。
ALLOWED_RUN_SYNC = {
    "core/db.py",  # 定义
    "core/sql.py",  # re-export
    "core/mysql_connection.py",  # re-export
    "core/process_user.py",  # *_sync 兼容包装（没有调用者，待移除）
    "core/group_chat_history.py",  # get_group_context 同步入口（没有调用者）
}


@pytest.mark.slow
def test_run_sync_stays_at_documented_sync_boundaries():
    offenders = []
    for relative, tree in _parsed_modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _dotted(node.func).endswith("run_sync"):
                if relative not in ALLOWED_RUN_SYNC:
                    offenders.append(f"{relative}:{node.lineno}")

    assert offenders == []


@pytest.mark.slow
def test_the_conversation_path_does_not_bridge_back_into_the_loop_from_threads():
    """`run_coroutine_threadsafe` 只剩 core.db.run_sync 一处：主路径没有跨线程的同步等待。"""
    offenders = []
    for relative, tree in _parsed_modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _dotted(node.func).endswith("run_coroutine_threadsafe"):
                if relative != "core/db.py":
                    offenders.append(f"{relative}:{node.lineno}")

    assert offenders == []


# -- 行为：回调里需要的同步网络调用在适配器线程里执行，且不阻塞事件循环 ----------------------------


@pytest.fixture(autouse=True)
def clean_adapters():
    blocking.reopen_all()
    yield
    blocking.shutdown_all()
    blocking.reopen_all()


async def _ticker(counter, stop):
    while not stop.is_set():
        counter["ticks"] += 1
        await asyncio.sleep(0.01)


@pytest.mark.slow
def test_the_monitoring_price_check_runs_off_the_event_loop(monkeypatch):
    seen = {}
    sent = []

    def fake_check_result(trigger_time, trigger_price):
        seen["thread"] = threading.get_ident()
        time.sleep(0.2)  # 同步的 binance 请求
        return f"checked {trigger_price}"

    async def fake_send(message):
        sent.append(message)

    async def immediate_sleep(_):
        return None

    monkeypatch.setattr(biance_api, "check_result", fake_check_result)
    monkeypatch.setattr(monitoring, "send_message_to_group", fake_send)
    monkeypatch.setattr(monitoring, "asyncio", SimpleNamespace(sleep=immediate_sleep))

    async def scenario():
        counter = {"ticks": 0}
        stop = asyncio.Event()
        ticker = asyncio.create_task(_ticker(counter, stop))
        seen["loop"] = threading.get_ident()
        await monitoring.delayed_check_result(1.0, 65000.0)
        stop.set()
        await ticker
        return counter["ticks"]

    ticks = asyncio.run(scenario())

    assert sent == ["checked 65000.0"]
    assert seen["thread"] != seen["loop"]
    assert ticks >= 10  # 同步请求 0.2 秒期间事件循环一直在转


def test_the_monitor_loop_polls_in_a_worker_thread(monkeypatch):
    seen = {}

    def fake_monitor():
        seen["thread"] = threading.get_ident()
        return [], None

    async def stop_after_one_pass(_):
        monitoring.monitor_thread = None

    monkeypatch.setattr(biance_api, "monitor_btc_pattern", fake_monitor)
    monkeypatch.setattr(monitoring, "asyncio", SimpleNamespace(sleep=stop_after_one_pass))
    monkeypatch.setattr(monitoring, "lock_until", 0)
    monkeypatch.setattr(monitoring, "monitor_thread", object())

    async def scenario():
        seen["loop"] = threading.get_ident()
        await monitoring.run_monitor_with_notification()

    asyncio.run(scenario())

    assert seen["thread"] != seen["loop"]


@pytest.mark.slow
def test_the_btc_price_lookup_does_not_block_the_event_loop(monkeypatch):
    seen = {}

    class FakeClient:
        def mark_price(self, symbol):
            seen["thread"] = threading.get_ident()
            time.sleep(0.2)
            return {"markPrice": "64321.5"}

    monkeypatch.setattr(crypto_predict, "UMFutures", FakeClient)

    async def scenario():
        counter = {"ticks": 0}
        stop = asyncio.Event()
        ticker = asyncio.create_task(_ticker(counter, stop))
        seen["loop"] = threading.get_ident()
        result = await crypto_predict.get_btc_price()
        stop.set()
        await ticker
        return result, counter["ticks"]

    (price, error), ticks = asyncio.run(scenario())

    assert (price, error) == (64321.5, None)
    assert seen["thread"] != seen["loop"]
    assert ticks >= 10


def test_a_failing_price_lookup_returns_a_safe_error_instead_of_raising(monkeypatch):
    class BrokenClient:
        def mark_price(self, symbol):
            raise ConnectionError("https://fapi.example/?signature=SECRETSIG")

    monkeypatch.setattr(crypto_predict, "UMFutures", BrokenClient)

    price, error = asyncio.run(crypto_predict.get_btc_price())

    assert price is None
    assert "SECRETSIG" not in error


def test_sticker_metadata_lookups_run_in_the_bounded_adapter(monkeypatch):
    seen = {}

    def fake_sticker_exists(pack_name, emoji):
        seen["thread"] = threading.get_ident()
        time.sleep(0.05)  # urllib 的同步请求
        return True

    monkeypatch.setattr(sticker_sender, "sticker_exists", fake_sticker_exists)

    async def scenario():
        seen["loop"] = threading.get_ident()
        return await sticker_sender.normalize_sticker_directives(
            "好呀 [sticker_pack:cat_pack emoji:😀]",
            logger=SimpleNamespace(info=lambda *a, **k: None),
        )

    text = asyncio.run(scenario())

    assert "[sticker_pack:cat_pack emoji:😀]" in text
    assert seen["thread"] != seen["loop"]
    assert Path(blocking.__file__).name == "blocking.py"
