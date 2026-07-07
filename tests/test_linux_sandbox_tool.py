import sys
import types

import pytest

from features.ai.tools import sandbox_tools
from features.ai.tools.context import clear_tool_request_context, set_tool_request_context


class _FakeCommandResult:
    def __init__(self, stdout="", stderr="", exit_code=0):
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code


class _FakeCommands:
    def __init__(self, sandbox):
        self.sandbox = sandbox

    def run(self, command, cwd=None, timeout=None):
        self.sandbox.calls.append({
            "command": command,
            "cwd": cwd,
            "timeout": timeout,
        })
        return _FakeCommandResult(stdout=f"ran: {command}\n")


class _FakeSandbox:
    created = []
    fail_kill = False

    def __init__(self):
        self.calls = []
        self.killed = False
        self.fail_kill = self.__class__.fail_kill
        self.commands = _FakeCommands(self)

    @classmethod
    def create(cls, **kwargs):
        sandbox = cls()
        sandbox.create_kwargs = kwargs
        cls.created.append(sandbox)
        return sandbox

    def kill(self):
        self.killed = True
        if self.fail_kill:
            raise RuntimeError("kill failed")


def _install_fake_e2b(monkeypatch):
    _FakeSandbox.created = []
    _FakeSandbox.fail_kill = False
    fake_module = types.SimpleNamespace(Sandbox=_FakeSandbox)
    monkeypatch.setitem(sys.modules, "e2b", fake_module)


@pytest.fixture(autouse=True)
def _clear_sandbox_state():
    sandbox_tools._ACTIVE_SANDBOX_USER_IDS.clear()
    sandbox_tools._USER_SANDBOX_CREATE_COOLDOWNS.clear()
    yield
    sandbox_tools._ACTIVE_SANDBOX_USER_IDS.clear()
    sandbox_tools._USER_SANDBOX_CREATE_COOLDOWNS.clear()


def test_linux_sandbox_tool_reports_unavailable_without_api_key(monkeypatch):
    monkeypatch.setattr(sandbox_tools.config, "E2B_API_KEY", "")

    result = sandbox_tools.linux_sandbox_tool("echo hello")

    assert result == {
        "status": "unavailable",
        "error": "Linux sandbox is not configured. Missing E2B_API_KEY.",
    }


def test_linux_sandbox_tool_reuses_sandbox_within_request(monkeypatch):
    _install_fake_e2b(monkeypatch)
    monkeypatch.setattr(sandbox_tools.config, "E2B_API_KEY", "e2b_test")
    set_tool_request_context({"user_id": 123})
    try:
        first = sandbox_tools.linux_sandbox_tool("echo one")
        second = sandbox_tools.linux_sandbox_tool(
            "echo two",
            cwd="/tmp",
            timeout_seconds=30,
        )

        assert first["status"] == "ok"
        assert first["stdout"] == "ran: echo one\n"
        assert second["status"] == "ok"
        assert len(_FakeSandbox.created) == 1
        sandbox = _FakeSandbox.created[0]
        assert sandbox.create_kwargs == {
            "api_key": "e2b_test",
            "timeout": sandbox_tools.MAX_SANDBOX_LIFETIME_SECONDS,
        }
        assert sandbox.calls == [
            {
                "command": "echo one",
                "cwd": "/home/user",
                "timeout": 15,
            },
            {
                "command": "echo two",
                "cwd": "/tmp",
                "timeout": 30,
            },
        ]
    finally:
        sandbox_tools.cleanup_linux_sandbox()
        clear_tool_request_context()


def test_cleanup_linux_sandbox_kills_request_sandbox(monkeypatch):
    _install_fake_e2b(monkeypatch)
    monkeypatch.setattr(sandbox_tools.config, "E2B_API_KEY", "e2b_test")
    set_tool_request_context({"user_id": 123})
    try:
        sandbox_tools.linux_sandbox_tool("echo hello")
        sandbox = _FakeSandbox.created[0]

        sandbox_tools.cleanup_linux_sandbox()

        assert sandbox.killed is True
        sandbox_tools.cleanup_linux_sandbox()
    finally:
        clear_tool_request_context()


def test_linux_sandbox_tool_blocks_background_commands(monkeypatch):
    monkeypatch.setattr(sandbox_tools.config, "E2B_API_KEY", "e2b_test")

    for command in ("sleep 10 &", "sleep 10 & echo done"):
        result = sandbox_tools.linux_sandbox_tool(command)

        assert result == {
            "status": "blocked",
            "error": "Background processes are not allowed in the sandbox tool.",
            "blocked_reason": "background_process",
        }


def test_linux_sandbox_tool_allows_non_background_ampersands(monkeypatch):
    _install_fake_e2b(monkeypatch)
    monkeypatch.setattr(sandbox_tools.config, "E2B_API_KEY", "e2b_test")
    set_tool_request_context({"user_id": 123})
    try:
        first = sandbox_tools.linux_sandbox_tool("echo one && echo two")
        second = sandbox_tools.linux_sandbox_tool('echo "a&b"')

        assert first["status"] == "ok"
        assert second["status"] == "ok"
        assert _FakeSandbox.created[0].calls[-2:] == [
            {
                "command": "echo one && echo two",
                "cwd": "/home/user",
                "timeout": 15,
            },
            {
                "command": 'echo "a&b"',
                "cwd": "/home/user",
                "timeout": 15,
            },
        ]
    finally:
        sandbox_tools.cleanup_linux_sandbox()
        clear_tool_request_context()


def test_linux_sandbox_tool_enforces_call_limit(monkeypatch):
    _install_fake_e2b(monkeypatch)
    monkeypatch.setattr(sandbox_tools.config, "E2B_API_KEY", "e2b_test")
    set_tool_request_context({"user_id": 123})
    try:
        for index in range(sandbox_tools.MAX_CALLS_PER_REQUEST):
            result = sandbox_tools.linux_sandbox_tool(f"echo {index}")
            assert result["status"] == "ok"

        blocked = sandbox_tools.linux_sandbox_tool("echo too-many")

        assert blocked == {
            "status": "blocked",
            "error": "Linux sandbox call limit reached for this request (10).",
            "blocked_reason": "call_limit",
        }
    finally:
        sandbox_tools.cleanup_linux_sandbox()
        clear_tool_request_context()


def test_linux_sandbox_tool_blocks_second_sandbox_for_same_user(monkeypatch):
    _install_fake_e2b(monkeypatch)
    monkeypatch.setattr(sandbox_tools.config, "E2B_API_KEY", "e2b_test")
    first_context = {"user_id": 123}
    second_context = {"user_id": 123}
    try:
        set_tool_request_context(first_context)
        first = sandbox_tools.linux_sandbox_tool("echo first")
        assert first["status"] == "ok"

        set_tool_request_context(second_context)
        blocked = sandbox_tools.linux_sandbox_tool("echo second")

        assert blocked == {
            "status": "blocked",
            "error": "A Linux sandbox is already running for this user. Try again after the current request finishes.",
            "blocked_reason": "user_sandbox_busy",
        }
        assert len(_FakeSandbox.created) == 1
    finally:
        set_tool_request_context(first_context)
        sandbox_tools.cleanup_linux_sandbox()
        clear_tool_request_context()


def test_linux_sandbox_tool_allows_different_users(monkeypatch):
    _install_fake_e2b(monkeypatch)
    monkeypatch.setattr(sandbox_tools.config, "E2B_API_KEY", "e2b_test")
    first_context = {"user_id": 123}
    second_context = {"user_id": 456}
    try:
        set_tool_request_context(first_context)
        first = sandbox_tools.linux_sandbox_tool("echo first")
        assert first["status"] == "ok"

        set_tool_request_context(second_context)
        second = sandbox_tools.linux_sandbox_tool("echo second")

        assert second["status"] == "ok"
        assert len(_FakeSandbox.created) == 2
    finally:
        set_tool_request_context(second_context)
        sandbox_tools.cleanup_linux_sandbox()
        set_tool_request_context(first_context)
        sandbox_tools.cleanup_linux_sandbox()
        clear_tool_request_context()


def test_linux_sandbox_tool_rate_limits_user_creations(monkeypatch):
    _install_fake_e2b(monkeypatch)
    monkeypatch.setattr(sandbox_tools.config, "E2B_API_KEY", "e2b_test")
    monkeypatch.setattr(sandbox_tools.time, "monotonic", lambda: 10_000.0)
    first_context = {"user_id": 123}
    second_context = {"user_id": 123}
    try:
        set_tool_request_context(first_context)
        first = sandbox_tools.linux_sandbox_tool("echo first")
        assert first["status"] == "ok"
        sandbox_tools.cleanup_linux_sandbox()

        set_tool_request_context(second_context)
        blocked = sandbox_tools.linux_sandbox_tool("echo second")

        assert blocked["status"] == "blocked"
        assert blocked["blocked_reason"] == "user_rate_limit"
        assert blocked["retry_after_seconds"] == sandbox_tools.USER_SANDBOX_CREATE_COOLDOWN_SECONDS
        assert len(_FakeSandbox.created) == 1
    finally:
        sandbox_tools._ACTIVE_SANDBOX_USER_IDS.clear()
        sandbox_tools._USER_SANDBOX_CREATE_COOLDOWNS.clear()
        clear_tool_request_context()


def test_linux_sandbox_tool_allows_user_creation_after_rate_limit_expires(monkeypatch):
    _install_fake_e2b(monkeypatch)
    monkeypatch.setattr(sandbox_tools.config, "E2B_API_KEY", "e2b_test")
    now = 10_000.0
    monkeypatch.setattr(sandbox_tools.time, "monotonic", lambda: now)
    first_context = {"user_id": 123}
    second_context = {"user_id": 123}
    try:
        set_tool_request_context(first_context)
        first = sandbox_tools.linux_sandbox_tool("echo first")
        assert first["status"] == "ok"
        sandbox_tools.cleanup_linux_sandbox()

        now += sandbox_tools.USER_SANDBOX_CREATE_COOLDOWN_SECONDS + 1

        set_tool_request_context(second_context)
        second = sandbox_tools.linux_sandbox_tool("echo second")

        assert second["status"] == "ok"
        assert len(_FakeSandbox.created) == 2
    finally:
        sandbox_tools.cleanup_linux_sandbox()
        sandbox_tools._ACTIVE_SANDBOX_USER_IDS.clear()
        sandbox_tools._USER_SANDBOX_CREATE_COOLDOWNS.clear()
        clear_tool_request_context()


def test_cleanup_linux_sandbox_keeps_user_lock_when_kill_fails(monkeypatch):
    _install_fake_e2b(monkeypatch)
    _FakeSandbox.fail_kill = True
    monkeypatch.setattr(sandbox_tools.config, "E2B_API_KEY", "e2b_test")
    first_context = {"user_id": 123}
    second_context = {"user_id": 123}
    try:
        set_tool_request_context(first_context)
        first = sandbox_tools.linux_sandbox_tool("echo first")
        assert first["status"] == "ok"

        sandbox_tools.cleanup_linux_sandbox()
        assert _FakeSandbox.created[0].killed is True

        set_tool_request_context(second_context)
        blocked = sandbox_tools.linux_sandbox_tool("echo second")

        assert blocked == {
            "status": "blocked",
            "error": "A Linux sandbox is already running for this user. Try again after the current request finishes.",
            "blocked_reason": "user_sandbox_busy",
        }
        assert len(_FakeSandbox.created) == 1
    finally:
        sandbox_tools._ACTIVE_SANDBOX_USER_IDS.clear()
        sandbox_tools._USER_SANDBOX_CREATE_COOLDOWNS.clear()
        clear_tool_request_context()
