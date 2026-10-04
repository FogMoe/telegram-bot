"""租约运行器与撤销信号：不需要数据库，数据库侧的行为在 tests/integration 里验证。"""

import asyncio
import logging
import threading

import pytest

from features.ai import job_claims, telegram_visible_sender, tool_runner
from features.ai.types import ABORT_EVENT_KEY, JobAbortedError, raise_if_aborted


def run(coro):
    return asyncio.run(coro)


async def _renew_always():
    return True


class TestRunLeased:
    def test_returns_the_result_and_stops_the_heartbeat(self):
        renewals = []

        async def renew():
            renewals.append(True)
            return True

        async def work():
            await asyncio.sleep(0.12)
            return "done"

        result = run(
            job_claims.run_leased(
                work(),
                renew=renew,
                lease_seconds=10,
                heartbeat_seconds=0.03,
                timeout=None,
            )
        )

        assert result == "done"
        assert len(renewals) >= 2

        before = len(renewals)
        run(asyncio.sleep(0.1))
        assert len(renewals) == before

    def test_work_exceptions_propagate_unchanged(self):
        async def work():
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            run(
                job_claims.run_leased(
                    work(),
                    renew=_renew_always,
                    lease_seconds=10,
                    heartbeat_seconds=10,
                    timeout=None,
                )
            )

    def test_rejected_renewal_cancels_the_worker_and_raises(self):
        abort_event = threading.Event()
        observed = {}

        async def renew():
            return False

        async def work():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                observed["cancelled"] = True
                raise

        with pytest.raises(job_claims.ClaimLostError):
            run(
                job_claims.run_leased(
                    work(),
                    renew=renew,
                    lease_seconds=10,
                    heartbeat_seconds=0.02,
                    timeout=None,
                    abort_event=abort_event,
                )
            )

        assert observed["cancelled"] is True
        assert abort_event.is_set()

    def test_a_transient_renewal_error_is_survived(self):
        calls = []

        async def renew():
            calls.append(True)
            if len(calls) == 1:
                raise ConnectionError("database restarting")
            return True

        async def work():
            await asyncio.sleep(0.15)
            return "done"

        result = run(
            job_claims.run_leased(
                work(),
                renew=renew,
                lease_seconds=10,
                heartbeat_seconds=0.03,
                timeout=None,
            )
        )

        assert result == "done" and len(calls) >= 2

    def test_a_lease_that_cannot_be_confirmed_for_a_whole_term_is_treated_as_lost(self):
        async def renew():
            raise ConnectionError("database is down")

        async def work():
            await asyncio.Event().wait()

        with pytest.raises(job_claims.ClaimLostError):
            run(
                job_claims.run_leased(
                    work(),
                    renew=renew,
                    lease_seconds=0.1,
                    heartbeat_seconds=0.02,
                    timeout=None,
                )
            )

    def test_execution_limit_cancels_the_worker_and_raises_timeout(self):
        abort_event = threading.Event()

        async def work():
            await asyncio.Event().wait()

        with pytest.raises(TimeoutError):
            run(
                job_claims.run_leased(
                    work(),
                    renew=_renew_always,
                    lease_seconds=10,
                    heartbeat_seconds=10,
                    timeout=0.05,
                    abort_event=abort_event,
                )
            )

        assert abort_event.is_set()

    def test_cancelling_the_caller_cancels_the_worker(self):
        abort_event = threading.Event()
        observed = {}

        async def work():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                observed["cancelled"] = True
                raise

        async def scenario():
            task = asyncio.create_task(
                job_claims.run_leased(
                    work(),
                    renew=_renew_always,
                    lease_seconds=10,
                    heartbeat_seconds=10,
                    timeout=None,
                    abort_event=abort_event,
                )
            )
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        run(scenario())

        assert observed["cancelled"] is True
        assert abort_event.is_set()


class TestTokens:
    def test_every_claim_gets_a_distinct_token(self):
        tokens = {job_claims.new_claim_token() for _ in range(100)}

        assert len(tokens) == 100
        assert all(len(token) == 32 for token in tokens)


class TestAbortSignal:
    def test_raise_if_aborted_only_fires_once_the_event_is_set(self):
        event = threading.Event()
        context = {ABORT_EVENT_KEY: event}

        raise_if_aborted(context)
        raise_if_aborted(None)
        raise_if_aborted({})
        event.set()
        with pytest.raises(JobAbortedError):
            raise_if_aborted(context)

    def test_aborting_is_not_an_exception_the_router_would_count_as_a_provider_failure(self):
        assert not issubclass(JobAbortedError, Exception)

    def test_tool_loop_stops_before_the_next_tool_and_model_call_once_aborted(self, monkeypatch):
        class Message:
            def __init__(self, content="", tool_calls=None):
                self.content = content
                self.tool_calls = tool_calls

        class Response:
            def __init__(self, message):
                self.choices = [type("Choice", (), {"message": message})()]

        event = threading.Event()
        tool_calls = [
            {"id": f"call_{index}", "type": "function",
             "function": {"name": "google_search", "arguments": '{"query": "x"}'}}
            for index in (1, 2)
        ]
        completions = []
        executed = []

        def fake_completion(*args, **kwargs):
            completions.append(True)
            return Response(Message("", tool_calls))

        def first_tool_loses_the_claim(**kwargs):
            executed.append(kwargs)
            event.set()
            return {"organic_results": []}

        monkeypatch.setattr(tool_runner, "create_chat_completion", fake_completion)
        monkeypatch.setitem(tool_runner.AI_TOOL_HANDLERS, "google_search", first_tool_loses_the_claim)

        with pytest.raises(JobAbortedError):
            tool_runner.run_tool_loop(
                "test_provider",
                "test_model",
                [{"role": "user", "content": "search"}],
                {ABORT_EVENT_KEY: event},
            )

        # 第一个工具已经执行（无法撤回），第二个工具和下一轮模型调用都没有发生。
        assert len(executed) == 1
        assert len(completions) == 1

    def test_tool_loop_without_an_abort_event_is_unaffected(self, monkeypatch):
        class Message:
            content = "done"
            tool_calls = None

        class Response:
            choices = [type("Choice", (), {"message": Message()})()]

        monkeypatch.setattr(
            tool_runner, "create_chat_completion", lambda *args, **kwargs: Response()
        )

        message, _ = tool_runner.run_tool_loop(
            "test_provider",
            "test_model",
            [{"role": "user", "content": "hello"}],
            {"user_id": 1},
        )

        assert message == "done"

    def test_visible_content_handler_stops_sending_once_aborted(self):
        sent = []

        async def scenario():
            event = threading.Event()
            handler = telegram_visible_sender.TelegramVisibleContentHandler(
                loop=asyncio.get_running_loop(),
                bot=object(),
                chat_id=1,
                first_text_send=lambda *a, **k: None,
                fallback_send=lambda *a, **k: None,
                logger=logging.getLogger(__name__),
                abort_event=event,
            )

            async def would_send(content):
                sent.append(content)
                return content

            handler._send = would_send  # type: ignore[method-assign]
            event.set()
            # __call__ 在工具循环所在的线程里被调用。
            with pytest.raises(JobAbortedError):
                await asyncio.to_thread(handler, "hello")
            with pytest.raises(JobAbortedError):
                await asyncio.to_thread(handler.send_tool_media, "generate_image", {})

        run(scenario())

        assert sent == []
