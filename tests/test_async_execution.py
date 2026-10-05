"""原生 async 的工具循环：工具派发、配对、可见内容即时投递、截止时间与租约取消。

模型调用与工具都用替身，不连网络。
"""

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from core import blocking, metrics
from core.deadline import REASON_SHUTDOWN, Deadline
from features.ai import job_claims, router, tool_runner
from features.ai.generated_image_sender import _collect_generated_images
from features.ai.tool_history import tool_logs_to_record_entries
from features.ai.tools.dispatch import inline_tool
from features.ai.types import (
    ABORT_EVENT_KEY,
    TOOL_CONTEXT_MESSAGES_KEY,
    TurnDeadlineError,
    media_delivery_attempted,
)


class Message:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class Response:
    def __init__(self, message):
        self.choices = [SimpleNamespace(message=message)]


def call(call_id, name="google_search", arguments='{"query": "x"}', **extra):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}, **extra}


def script(*responses):
    """按顺序返回响应的模型替身，并记录每次调用的参数。"""
    queue = list(responses)
    calls = []

    async def fake_create_chat_completion(provider, model, *, messages, **kwargs):
        calls.append(SimpleNamespace(provider=provider, model=model, messages=list(messages), kwargs=kwargs))
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return await item()
        return item

    return fake_create_chat_completion, calls


def run_loop(coro):
    return asyncio.run(coro)


async def stateless_one_tool_call(provider, model, *, messages, **kwargs):
    """无状态的模型替身：没有工具结果就发起一次工具调用，有了就回答。多个会话可以共用。"""
    if any(message.get("role") == "tool" for message in messages):
        return Response(Message("done"))
    return Response(Message("", [call("c1")]))


@pytest.fixture(autouse=True)
def isolated_runtime():
    metrics.REGISTRY.reset()
    blocking.shutdown_all()
    blocking.reopen_all()
    yield
    blocking.shutdown_all()
    blocking.reopen_all()
    metrics.REGISTRY.reset()


def loop_with(monkeypatch, fake, handlers, **kwargs):
    monkeypatch.setattr(tool_runner, "create_chat_completion", fake)
    for name, handler in handlers.items():
        monkeypatch.setitem(tool_runner.AI_TOOL_HANDLERS, name, handler)
    return tool_runner.run_tool_loop(
        "openai",
        "test-model",
        kwargs.pop("messages", [{"role": "user", "content": "go"}]),
        kwargs.pop("tool_context", None),
        provider_name="Test",
        **kwargs,
    )


# -- 工具派发 ----------------------------------------------------------------------------


def test_async_and_sync_tools_can_be_mixed_in_one_round_and_stay_paired(monkeypatch):
    fake, calls = script(
        Response(Message("", [call("c1", "google_search"), call("c2", "fetch_url", '{"url": "https://e.test"}')])),
        Response(Message("done")),
    )
    seen = {}

    async def async_search(**kwargs):
        seen["async_thread"] = threading.get_ident()
        return {"organic_results": ["a"]}

    def sync_fetch(**kwargs):
        seen["sync_thread"] = threading.get_ident()
        return {"content": "page"}

    async def scenario():
        seen["loop_thread"] = threading.get_ident()
        return await loop_with(monkeypatch, fake, {"google_search": async_search, "fetch_url": sync_fetch})

    message, tool_logs = run_loop(scenario())

    assert message == "done"
    assert seen["async_thread"] == seen["loop_thread"]  # async 工具直接在事件循环里 await
    assert seen["sync_thread"] != seen["loop_thread"]  # 同步工具在线程适配器里
    results = [log for log in tool_logs if log["type"] == "tool_result"]
    assert [log["tool_call_id"] for log in results] == ["c1", "c2"]
    # 第二次模型调用看到的 assistant 消息带着两个 tool_calls，后面跟着两条配对的 tool 消息。
    second_call_messages = calls[1].messages
    assistant = next(m for m in second_call_messages if m.get("tool_calls"))
    assert [c["id"] for c in assistant["tool_calls"]] == ["c1", "c2"]
    assert [m["tool_call_id"] for m in second_call_messages if m.get("role") == "tool"] == ["c1", "c2"]
    entries = tool_logs_to_record_entries(tool_logs)
    assert [role for role, _ in entries] == ["assistant", "tool", "tool"]


def test_multiple_tool_rounds_run_until_the_model_answers(monkeypatch):
    fake, calls = script(
        Response(Message("", [call("c1")])),
        Response(Message("", [call("c2", "fetch_url", '{"url": "https://e.test"}')])),
        Response(Message("final")),
    )
    order = []

    async def search(**kwargs):
        order.append("search")
        return {"ok": True}

    def fetch(**kwargs):
        order.append("fetch")
        return {"ok": True}

    message, tool_logs = run_loop(
        loop_with(monkeypatch, fake, {"google_search": search, "fetch_url": fetch})
    )

    assert message == "final"
    assert order == ["search", "fetch"]
    assert len(calls) == 3
    assert [log["tool_call_id"] for log in tool_logs if log["type"] == "tool_result"] == ["c1", "c2"]


@pytest.mark.slow
def test_sync_tool_concurrency_is_bounded_by_the_thread_adapter(monkeypatch, settings_override):
    settings_override(BLOCKING_TOOL_THREADS=2)
    blocking.shutdown_all()
    blocking.reopen_all()
    lock = threading.Lock()
    state = {"running": 0, "peak": 0}

    def slow_sync_tool(**kwargs):
        with lock:
            state["running"] += 1
            state["peak"] = max(state["peak"], state["running"])
        time.sleep(0.05)
        with lock:
            state["running"] -= 1
        return {"ok": True}

    async def one_conversation():
        return await loop_with(
            monkeypatch, stateless_one_tool_call, {"google_search": slow_sync_tool}
        )

    async def scenario():
        return await asyncio.gather(*(one_conversation() for _ in range(6)))

    results = run_loop(scenario())

    assert all(message == "done" for message, _ in results)
    assert state["peak"] == 2


def test_async_tools_do_not_use_the_thread_pool(monkeypatch, settings_override):
    settings_override(BLOCKING_TOOL_THREADS=1)
    blocking.shutdown_all()
    blocking.reopen_all()
    running = {"now": 0, "peak": 0}

    async def slow_async_tool(**kwargs):
        running["now"] += 1
        running["peak"] = max(running["peak"], running["now"])
        await asyncio.sleep(0.05)
        running["now"] -= 1
        return {"ok": True}

    async def one_conversation():
        return await loop_with(
            monkeypatch, stateless_one_tool_call, {"google_search": slow_async_tool}
        )

    async def scenario():
        return await asyncio.gather(*(one_conversation() for _ in range(5)))

    run_loop(scenario())

    assert running["peak"] == 5  # 一个线程的池子也不限制 async 工具
    assert blocking.tools().queued == 0


def test_an_inline_tool_runs_on_the_event_loop_without_a_thread_hop(monkeypatch):
    fake, _ = script(Response(Message("", [call("c1", "get_help_text", "{}")])), Response(Message("ok")))
    seen = {}

    @inline_tool
    def help_text(**kwargs):
        seen["thread"] = threading.get_ident()
        return {"help_text": "..."}

    async def scenario():
        seen["loop"] = threading.get_ident()
        return await loop_with(monkeypatch, fake, {"get_help_text": help_text})

    run_loop(scenario())

    assert seen["thread"] == seen["loop"]


def test_the_request_context_reaches_sync_tools_running_in_threads(monkeypatch):
    from features.ai.tools.context import get_tool_request_context, set_tool_request_context

    fake, _ = script(Response(Message("", [call("c1")])), Response(Message("ok")))
    seen = {}

    def sync_tool(**kwargs):
        seen["context"] = dict(get_tool_request_context())
        return {"ok": True}

    async def scenario():
        set_tool_request_context({"user_id": 77})
        return await loop_with(monkeypatch, fake, {"google_search": sync_tool})

    run_loop(scenario())

    assert seen["context"] == {"user_id": 77}


def test_tool_failures_and_calls_are_counted_per_registered_tool(monkeypatch):
    fake, _ = script(
        Response(Message("", [call("c1"), call("c2", "made_up_tool", "{}")])),
        Response(Message("ok")),
    )

    async def failing(**kwargs):
        return {"error": "upstream down"}

    run_loop(loop_with(monkeypatch, fake, {"google_search": failing}))

    snapshot = metrics.snapshot()
    assert snapshot.counter("tool.calls", tool="google_search") == 1
    assert snapshot.counter("tool.failures", tool="google_search") == 1
    # 模型编造的工具名不会成为指标标签。
    assert snapshot.counter("tool.calls", tool="made_up_tool") == 0


# -- 可见内容与媒体 ------------------------------------------------------------------------


def test_visible_content_is_delivered_before_the_tool_it_precedes_runs(monkeypatch):
    fake, _ = script(
        Response(Message("我先查一下。", [call("c1")])),
        Response(Message("查到了。")),
    )
    events = []

    class Handler:
        sent_contents: list[str] = []

        async def __call__(self, content):
            events.append(("visible", content))
            self.sent_contents.append(content)
            return content

    async def search(**kwargs):
        events.append(("tool", "google_search"))
        return {"ok": True}

    message, tool_logs = run_loop(
        loop_with(monkeypatch, fake, {"google_search": search}, visible_content_handler=Handler())
    )

    assert events == [
        ("visible", "我先查一下。"),
        ("tool", "google_search"),
        ("visible", "查到了。"),
    ]
    assert message == ""  # 已经即时发送，最终回复不再重复
    assert [log["content"] for log in tool_logs if log["type"] == "assistant_visible"] == [
        "我先查一下。",
        "查到了。",
    ]


def test_generated_media_is_sent_right_after_the_tool_that_produced_it(monkeypatch):
    fake, _ = script(Response(Message("", [call("c1", "generate_image", '{"prompt": "cat"}')])), Response(Message("好了")))
    sent = []

    class Handler:
        async def __call__(self, content):
            return content

        async def send_tool_media(self, tool_name, result):
            sent.append(tool_name)
            return ["photo"]

    def make_image(**kwargs):
        return {"status": "generated", "image": {"image_id": "i"}}

    message, tool_logs = run_loop(
        loop_with(monkeypatch, fake, {"generate_image": make_image}, visible_content_handler=Handler())
    )

    assert sent == ["generate_image"]
    result = next(log for log in tool_logs if log["type"] == "tool_result")
    assert result["media_sent"] is True


def test_provider_specific_fields_survive_the_round_trip(monkeypatch):
    signature = {"thought_signature": "opaque-signature"}
    tool_call = call("c1", provider_specific_fields=signature)
    assistant = Message("", [tool_call])
    assistant.provider_specific_fields = {"thought": "kept"}
    fake, calls = script(Response(assistant), Response(Message("done")))

    async def search(**kwargs):
        return {"ok": True}

    _, tool_logs = run_loop(loop_with(monkeypatch, fake, {"google_search": search}))

    sent_assistant = next(m for m in calls[1].messages if m.get("tool_calls"))
    assert sent_assistant["tool_calls"][0]["provider_specific_fields"] == signature
    logged = next(log for log in tool_logs if log["type"] == "assistant_tool_call")
    assert logged["assistant_message"]["tool_calls"][0]["provider_specific_fields"] == signature


def test_skipped_tools_are_not_executed(monkeypatch):
    fake, _ = script(Response(Message("", [call("c1", "google_search")])), Response(Message("ok")))
    executed = []

    async def search(**kwargs):
        executed.append(True)
        return {}

    run_loop(loop_with(monkeypatch, fake, {"google_search": search}, skip_tools={"google_search"}))

    assert executed == []


# -- 整轮截止时间 --------------------------------------------------------------------------


@pytest.mark.slow
def test_a_hanging_provider_call_ends_at_the_deadline_with_a_typed_error(monkeypatch):
    async def hang():
        await asyncio.sleep(30)

    fake, _ = script(hang)

    async def scenario():
        started = time.monotonic()
        with pytest.raises(TurnDeadlineError) as exc_info:
            await loop_with(monkeypatch, fake, {}, deadline=Deadline(0.1))
        return time.monotonic() - started, exc_info.value

    elapsed, error = run_loop(scenario())

    assert elapsed < 1.5
    assert error.phase == "model" and error.reason == "deadline"
    assert error.tool_logs == []


def test_the_per_call_timeout_is_tightened_to_the_remaining_deadline(monkeypatch):
    fake, calls = script(Response(Message("hi")))

    run_loop(loop_with(monkeypatch, fake, {}, deadline=Deadline(42)))

    assert calls[0].kwargs["timeout"] <= 42


@pytest.mark.slow
def test_a_tool_cut_off_by_the_deadline_is_paired_and_never_replayed(monkeypatch):
    fake, calls = script(
        Response(Message("", [call("c1"), call("c2", "fetch_url", '{"url": "https://e.test"}')])),
    )
    executed = []

    async def hanging_search(**kwargs):
        executed.append("google_search")
        await asyncio.sleep(30)

    async def fetch(**kwargs):
        executed.append("fetch_url")
        return {}

    async def scenario():
        with pytest.raises(TurnDeadlineError) as exc_info:
            await loop_with(
                monkeypatch,
                fake,
                {"google_search": hanging_search, "fetch_url": fetch},
                deadline=Deadline(0.1),
            )
        return exc_info.value

    error = run_loop(scenario())

    assert error.phase == "tool"
    assert executed == ["google_search"]  # 后面的工具没有被执行
    results = {log["tool_call_id"]: log["result"] for log in error.tool_logs if log["type"] == "tool_result"}
    assert set(results) == {"c1", "c2"}  # 每个 tool_call 都有结果，历史仍然配对
    assert results["c1"]["error"] == "interrupted" and results["c1"]["outcome"] == "unknown"
    assert results["c2"]["error"] == "not_executed"
    entries = tool_logs_to_record_entries(error.tool_logs)
    assert [role for role, _ in entries] == ["assistant", "tool", "tool"]
    assert len(calls) == 1  # 没有为了「重试」再调用模型


@pytest.mark.slow
def test_a_sync_tool_cut_off_by_the_deadline_is_abandoned_not_waited_for(monkeypatch):
    fake, _ = script(Response(Message("", [call("c1")])))
    release = threading.Event()
    finished = threading.Event()

    def slow_sync_tool(**kwargs):
        release.wait(5)
        finished.set()
        return {"ok": True}

    async def scenario():
        started = time.monotonic()
        with pytest.raises(TurnDeadlineError) as exc_info:
            await loop_with(monkeypatch, fake, {"google_search": slow_sync_tool}, deadline=Deadline(0.1))
        return time.monotonic() - started, exc_info.value

    try:
        elapsed, error = run_loop(scenario())
        assert elapsed < 1.5  # 不等线程
        assert error.phase == "tool"
        assert not finished.is_set()
    finally:
        release.set()
    assert finished.wait(2)  # 线程自己跑完，结果被丢弃


@pytest.mark.slow
def test_the_deadline_also_covers_visible_content_delivery(monkeypatch):
    fake, _ = script(Response(Message("很长的一段话", None)))

    class StuckHandler:
        sent_contents: list[str] = []

        async def __call__(self, content):
            await asyncio.sleep(30)

    async def scenario():
        with pytest.raises(TurnDeadlineError) as exc_info:
            await loop_with(
                monkeypatch, fake, {}, visible_content_handler=StuckHandler(), deadline=Deadline(0.1)
            )
        return exc_info.value

    assert run_loop(scenario()).phase == "delivery"


@pytest.mark.slow
def test_media_whose_delivery_is_cut_off_keeps_its_result_and_is_not_sent_again(monkeypatch):
    fake, _ = script(
        Response(
            Message(
                "",
                [call("c1", "generate_image", '{"prompt": "cat"}'), call("c2")],
            )
        ),
    )
    executed = []

    class StuckMediaHandler:
        sent_contents: list[str] = []

        async def __call__(self, content):
            return content

        async def send_tool_media(self, tool_name, result):
            await asyncio.sleep(30)

    def make_image(**kwargs):
        executed.append("generate_image")
        return {"status": "generated", "images": [{"image_id": "i"}]}

    async def search(**kwargs):
        executed.append("google_search")
        return {}

    async def scenario():
        with pytest.raises(TurnDeadlineError) as exc_info:
            await loop_with(
                monkeypatch,
                fake,
                {"generate_image": make_image, "google_search": search},
                visible_content_handler=StuckMediaHandler(),
                deadline=Deadline(0.1),
            )
        return exc_info.value

    error = run_loop(scenario())

    assert error.phase == "delivery"
    assert executed == ["generate_image"]
    results = {log["tool_call_id"]: log for log in error.tool_logs if log["type"] == "tool_result"}
    assert set(results) == {"c1", "c2"}
    # 生成成功的结果留在历史里，下一轮知道媒体已经生成；投递阶段也不会再发一次。
    assert results["c1"]["result"]["status"] == "generated"
    assert "may or may not have arrived" in results["c1"]["result"]["message"]
    assert results["c1"]["internal_result"]["status"] == "generated"
    assert media_delivery_attempted(results["c1"])
    assert _collect_generated_images(error.tool_logs) == []
    assert results["c2"]["result"]["error"] == "not_executed"
    assert [role for role, _ in tool_logs_to_record_entries(error.tool_logs)] == [
        "assistant",
        "tool",
        "tool",
    ]


@pytest.mark.slow
def test_telegram_events_of_tools_that_finished_survive_a_deadline_later_in_the_round(monkeypatch):
    fake, _ = script(
        Response(Message("", [call("c1", "fetch_url", '{"url": "https://e.test"}'), call("c2")])),
    )

    async def command_like(**kwargs):
        return {"success": True, TOOL_CONTEXT_MESSAGES_KEY: ['<event type="bot_event">ok</event>']}

    async def hanging_search(**kwargs):
        await asyncio.sleep(30)

    async def scenario():
        with pytest.raises(TurnDeadlineError) as exc_info:
            await loop_with(
                monkeypatch,
                fake,
                {"fetch_url": command_like, "google_search": hanging_search},
                deadline=Deadline(0.1),
            )
        return exc_info.value

    error = run_loop(scenario())

    events = [log["content"] for log in error.tool_logs if log["type"] == "telegram_event"]
    assert events == ['<event type="bot_event">ok</event>']
    results = {log["tool_call_id"]: log["result"] for log in error.tool_logs if log["type"] == "tool_result"}
    assert results["c1"] == {"success": True}
    assert results["c2"]["error"] == "interrupted"


# -- router：回退、截止时间与关停提示 -------------------------------------------------------------


@pytest.fixture
def two_providers(settings_override, monkeypatch):
    settings_override(
        AI_CHAT_ORDER="openai,gemini",
        OPENAI_CHAT_MODEL="openai-model",
        GEMINI_CHAT_MODEL="gemini-model",
    )
    router._provider_failure_streaks.clear()
    router._provider_circuit_open_until.clear()
    yield
    router._provider_failure_streaks.clear()
    router._provider_circuit_open_until.clear()


def test_a_failed_provider_falls_back_to_the_next_one_through_the_async_stack(monkeypatch, two_providers):
    fake, calls = script(RuntimeError("openai down"), Response(Message("来自 gemini")))
    monkeypatch.setattr(tool_runner, "create_chat_completion", fake)

    response = run_loop(router.get_ai_response([{"role": "user", "content": "hi"}], user_id=1))

    assert response == ("来自 gemini", [])
    assert [(c.provider, c.model) for c in calls] == [("openai", "openai-model"), ("gemini", "gemini-model")]


@pytest.mark.slow
def test_a_hung_provider_ends_the_whole_turn_at_the_deadline_with_a_notice(monkeypatch, two_providers):
    async def hang():
        await asyncio.sleep(30)

    fake, calls = script(hang, Response(Message("never used")))
    monkeypatch.setattr(tool_runner, "create_chat_completion", fake)

    async def scenario():
        started = time.monotonic()
        response = await router.get_ai_response(
            [{"role": "user", "content": "hi"}], user_id=1, deadline=Deadline(0.1)
        )
        return time.monotonic() - started, response

    elapsed, response = run_loop(scenario())

    assert elapsed < 1.5
    assert response == (router.TURN_DEADLINE_ERROR_MESSAGE, [])
    assert router.runtime_error_cause(response[0]) == "turn_deadline_exceeded"
    assert [c.provider for c in calls] == ["openai"]  # 没有再换下一个 provider
    assert metrics.snapshot().counter("turn.deadline_hits", reason="deadline", phase="model") == 1


def test_an_expired_deadline_never_calls_a_provider_or_blames_one(monkeypatch, two_providers):
    fake, calls = script(Response(Message("too late")))
    monkeypatch.setattr(tool_runner, "create_chat_completion", fake)

    response = run_loop(
        router.get_ai_response([{"role": "user", "content": "hi"}], user_id=1, deadline=Deadline(0.0))
    )

    assert response == (router.TURN_DEADLINE_ERROR_MESSAGE, [])
    assert calls == []
    # 从没被调用过的 provider 不会因为时间用完而被记一次失败。
    assert router._provider_failure_streaks == {}
    assert metrics.snapshot().counter("turn.deadline_hits", reason="deadline", phase="fallback") == 1


@pytest.mark.slow
def test_the_first_provider_that_runs_into_the_deadline_leaves_no_time_for_the_fallback(
    monkeypatch, two_providers
):
    async def slow_failure():
        await asyncio.sleep(0.15)
        raise RuntimeError("openai failed slowly")

    fake, calls = script(slow_failure, Response(Message("too late")))
    monkeypatch.setattr(tool_runner, "create_chat_completion", fake)

    response = run_loop(
        router.get_ai_response([{"role": "user", "content": "hi"}], user_id=1, deadline=Deadline(0.1))
    )

    assert response[0] == router.TURN_DEADLINE_ERROR_MESSAGE
    assert len(calls) == 1
    assert "openai" in router._provider_failure_streaks  # 正在等待的 provider 这次算超时
    assert "gemini" not in router._provider_failure_streaks


@pytest.mark.slow
def test_a_deadline_during_tools_returns_the_paired_tool_logs_and_does_not_retry(monkeypatch, two_providers):
    async def hanging_search(**kwargs):
        await asyncio.sleep(30)

    fake, calls = script(Response(Message("", [call("c1")])), Response(Message("never")))
    monkeypatch.setattr(tool_runner, "create_chat_completion", fake)
    monkeypatch.setitem(tool_runner.AI_TOOL_HANDLERS, "google_search", hanging_search)

    text, tool_logs = run_loop(
        router.get_ai_response([{"role": "user", "content": "hi"}], user_id=1, deadline=Deadline(0.1))
    )

    assert text == router.TURN_DEADLINE_ERROR_MESSAGE
    assert [log["type"] for log in tool_logs] == ["assistant_tool_call", "tool_result"]
    assert tool_logs[1]["result"]["error"] == "interrupted"
    assert len(calls) == 1


@pytest.mark.slow
def test_a_deadline_after_visible_content_still_tells_the_user(monkeypatch, two_providers):
    class Handler:
        sent_contents = ["已经发出的一段"]

        async def __call__(self, content):
            return content

    async def hang():
        await asyncio.sleep(30)

    fake, _ = script(hang)
    monkeypatch.setattr(tool_runner, "create_chat_completion", fake)

    text, _ = run_loop(
        router.get_ai_response(
            [{"role": "user", "content": "hi"}],
            user_id=1,
            visible_content_handler=Handler(),
            deadline=Deadline(0.1),
        )
    )

    assert text == router.TURN_DEADLINE_ERROR_MESSAGE


def test_shutdown_uses_its_own_notice_and_does_not_count_against_the_provider(monkeypatch, two_providers):
    async def hang():
        await asyncio.sleep(30)

    fake, _ = script(hang)
    monkeypatch.setattr(tool_runner, "create_chat_completion", fake)

    async def scenario():
        deadline = Deadline(60)

        async def stop_soon():
            await asyncio.sleep(0.05)
            deadline.expire(REASON_SHUTDOWN)

        stopper = asyncio.create_task(stop_soon())
        response = await router.get_ai_response(
            [{"role": "user", "content": "hi"}], user_id=1, deadline=deadline
        )
        await stopper
        return response

    response = run_loop(scenario())

    assert response[0] == router.TURN_SHUTDOWN_ERROR_MESSAGE
    assert router.runtime_error_cause(response[0]) == "turn_interrupted_by_shutdown"
    assert router._provider_failure_streaks == {}


def test_no_deadline_means_the_old_behaviour(monkeypatch, two_providers):
    fake, _ = script(Response(Message("plain")))
    monkeypatch.setattr(tool_runner, "create_chat_completion", fake)

    assert run_loop(router.get_ai_response([{"role": "user", "content": "hi"}], user_id=1)) == ("plain", [])


# -- E 的租约契约：原生 async 之后仍然成立 -----------------------------------------------------------


def test_lease_loss_cancels_a_hung_provider_call_natively_and_sets_the_abort_event(monkeypatch):
    cancelled = []
    event = threading.Event()

    async def hang():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    fake, _ = script(hang)
    monkeypatch.setattr(tool_runner, "create_chat_completion", fake)

    async def renew():
        return False  # claim 已经易主

    async def scenario():
        started = time.monotonic()
        with pytest.raises(job_claims.ClaimLostError):
            await job_claims.run_leased(
                tool_runner.run_tool_loop(
                    "openai",
                    "m",
                    [{"role": "user", "content": "x"}],
                    {ABORT_EVENT_KEY: event},
                    provider_name="Test",
                ),
                renew=renew,
                lease_seconds=10,
                heartbeat_seconds=0.05,
                timeout=None,
                abort_event=event,
            )
        return time.monotonic() - started

    elapsed = run_loop(scenario())

    assert cancelled == [True]  # 正在等待的模型调用被真正取消，而不是等它自己结束
    assert event.is_set()
    assert elapsed < 1.5
