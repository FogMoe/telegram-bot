import logging
import re
import threading
import time
from typing import Any, Optional

from core import config

from .context import get_tool_request_context

DEFAULT_CWD = "/home/user"
DEFAULT_COMMAND_TIMEOUT_SECONDS = 15
MAX_COMMAND_TIMEOUT_SECONDS = 60
MAX_COMMAND_CHARS = 2000
MAX_OUTPUT_CHARS = 12_000
MAX_CALLS_PER_REQUEST = 10
MAX_SANDBOX_LIFETIME_SECONDS = 300

_SANDBOX_CONTEXT_KEY = "_linux_sandbox"
_SANDBOX_CALL_COUNT_KEY = "_linux_sandbox_call_count"
_SANDBOX_USER_ID_KEY = "_linux_sandbox_user_id"
_ACTIVE_SANDBOX_USER_IDS: dict[str, float] = {}
_ACTIVE_SANDBOX_LOCK = threading.Lock()

_BACKGROUND_PROCESS_PATTERN = re.compile(
    r"(?:^|[;&|]\s*)(?:nohup|setsid|screen|tmux)\b",
    re.IGNORECASE,
)


class _SandboxBlockedError(Exception):
    def __init__(self, result: dict):
        super().__init__(result.get("error") or "Linux sandbox blocked")
        self.result = result


def _e2b_api_key() -> str:
    return (getattr(config, "E2B_API_KEY", "") or "").strip()


def _truncate_output(value: Any, remaining_chars: int) -> tuple[str, bool]:
    text = "" if value is None else str(value)
    if remaining_chars <= 0:
        return "", bool(text)
    if len(text) <= remaining_chars:
        return text, False
    return text[:remaining_chars], True


def _clamp_timeout(timeout_seconds: Optional[int]) -> int:
    try:
        timeout_value = int(timeout_seconds) if timeout_seconds is not None else DEFAULT_COMMAND_TIMEOUT_SECONDS
    except (TypeError, ValueError):
        timeout_value = DEFAULT_COMMAND_TIMEOUT_SECONDS
    return max(1, min(timeout_value, MAX_COMMAND_TIMEOUT_SECONDS))


def _normalise_cwd(cwd: Optional[str]) -> str:
    if not isinstance(cwd, str) or not cwd.strip():
        return DEFAULT_CWD
    return cwd.strip()


def _sandbox_unavailable_result() -> dict:
    return {
        "status": "unavailable",
        "error": "Linux sandbox is not configured. Missing E2B_API_KEY.",
    }


def _blocked_result(error: str, reason: str) -> dict:
    return {
        "status": "blocked",
        "error": error,
        "blocked_reason": reason,
    }


def _context_user_id(context: dict[str, object]) -> Optional[str]:
    user_id = context.get("user_id")
    if user_id is None:
        return None
    user_id_text = str(user_id).strip()
    return user_id_text or None


def _contains_unquoted_background_operator(command: str) -> bool:
    in_single_quote = False
    in_double_quote = False
    index = 0
    while index < len(command):
        char = command[index]
        if char == "\\" and not in_single_quote:
            index += 2
            continue
        if char == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
        elif char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
        elif char == "&" and not in_single_quote and not in_double_quote:
            next_char = command[index + 1] if index + 1 < len(command) else ""
            previous_char = command[index - 1] if index > 0 else ""
            if next_char == "&":
                index += 2
                continue
            if previous_char != "&":
                return True
        index += 1
    return False


def _background_process_detected(command: str) -> bool:
    return bool(
        _BACKGROUND_PROCESS_PATTERN.search(command)
        or _contains_unquoted_background_operator(command)
    )


def _purge_expired_user_locks(now: float) -> None:
    expired_user_ids = [
        user_id
        for user_id, expires_at in _ACTIVE_SANDBOX_USER_IDS.items()
        if expires_at <= now
    ]
    for user_id in expired_user_ids:
        _ACTIVE_SANDBOX_USER_IDS.pop(user_id, None)


def _reserve_user_sandbox(context: dict[str, object]) -> None:
    if context.get(_SANDBOX_USER_ID_KEY):
        return

    user_id = _context_user_id(context)
    if not user_id:
        raise _SandboxBlockedError(
            _blocked_result(
                "Missing user information, cannot create Linux sandbox.",
                "missing_user",
            )
        )

    now = time.monotonic()
    with _ACTIVE_SANDBOX_LOCK:
        _purge_expired_user_locks(now)
        if user_id in _ACTIVE_SANDBOX_USER_IDS:
            raise _SandboxBlockedError(
                _blocked_result(
                    "A Linux sandbox is already running for this user. Try again after the current request finishes.",
                    "user_sandbox_busy",
                )
            )
        _ACTIVE_SANDBOX_USER_IDS[user_id] = now + MAX_SANDBOX_LIFETIME_SECONDS
    context[_SANDBOX_USER_ID_KEY] = user_id


def _release_user_sandbox(context: dict[str, object]) -> None:
    user_id = context.pop(_SANDBOX_USER_ID_KEY, None)
    if not user_id:
        return
    with _ACTIVE_SANDBOX_LOCK:
        _ACTIVE_SANDBOX_USER_IDS.pop(str(user_id), None)


def _result_from_command(
    *,
    exit_code: Optional[int],
    stdout: Any,
    stderr: Any,
    cwd: str,
    timeout_seconds: int,
    status: Optional[str] = None,
) -> dict:
    stdout_text, stdout_truncated = _truncate_output(stdout, MAX_OUTPUT_CHARS)
    stderr_text, stderr_truncated = _truncate_output(
        stderr,
        MAX_OUTPUT_CHARS - len(stdout_text),
    )
    truncated = stdout_truncated or stderr_truncated
    return {
        "status": status or ("ok" if exit_code == 0 else "error"),
        "exit_code": exit_code,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "cwd": cwd,
        "timeout_seconds": timeout_seconds,
        "truncated": truncated,
        "output_limit_chars": MAX_OUTPUT_CHARS,
    }


def _get_or_create_sandbox(context: dict[str, object]):
    sandbox = context.get(_SANDBOX_CONTEXT_KEY)
    if sandbox is not None:
        return sandbox

    _reserve_user_sandbox(context)

    try:
        from e2b import Sandbox
    except ImportError:
        _release_user_sandbox(context)
        logging.exception("E2B SDK is not installed")
        raise RuntimeError("E2B SDK is not installed. Install the e2b package.") from None

    try:
        sandbox = Sandbox.create(
            api_key=_e2b_api_key(),
            timeout=MAX_SANDBOX_LIFETIME_SECONDS,
        )
        context[_SANDBOX_CONTEXT_KEY] = sandbox
        return sandbox
    except Exception:
        _release_user_sandbox(context)
        raise


def linux_sandbox_tool(
    command: str,
    cwd: Optional[str] = None,
    timeout_seconds: Optional[int] = None,
    **kwargs,
) -> dict:
    """Run a shell command in a temporary E2B Linux sandbox for this request."""
    if not _e2b_api_key():
        return _sandbox_unavailable_result()

    if not isinstance(command, str) or not command.strip():
        return _blocked_result("Shell command is required.", "missing_command")

    command_value = command.strip()
    if len(command_value) > MAX_COMMAND_CHARS:
        return _blocked_result(
            f"Command is too long. Maximum length is {MAX_COMMAND_CHARS} characters.",
            "command_too_long",
        )

    if _background_process_detected(command_value):
        return _blocked_result(
            "Background processes are not allowed in the sandbox tool.",
            "background_process",
        )

    request_context = get_tool_request_context()
    call_count = int(request_context.get(_SANDBOX_CALL_COUNT_KEY, 0) or 0)
    if call_count >= MAX_CALLS_PER_REQUEST:
        return _blocked_result(
            f"Linux sandbox call limit reached for this request ({MAX_CALLS_PER_REQUEST}).",
            "call_limit",
        )
    request_context[_SANDBOX_CALL_COUNT_KEY] = call_count + 1

    cwd_value = _normalise_cwd(cwd)
    timeout_value = _clamp_timeout(timeout_seconds)

    try:
        sandbox = _get_or_create_sandbox(request_context)
        result = sandbox.commands.run(
            command_value,
            cwd=cwd_value,
            timeout=timeout_value,
        )
        return _result_from_command(
            exit_code=getattr(result, "exit_code", None),
            stdout=getattr(result, "stdout", ""),
            stderr=getattr(result, "stderr", ""),
            cwd=cwd_value,
            timeout_seconds=timeout_value,
        )
    except _SandboxBlockedError as exc:
        return exc.result
    except Exception as exc:
        exit_code = getattr(exc, "exit_code", None)
        if exit_code is None:
            exit_code = getattr(exc, "code", None)
        stdout = getattr(exc, "stdout", "")
        stderr = getattr(exc, "stderr", "") or str(exc)
        return _result_from_command(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            cwd=cwd_value,
            timeout_seconds=timeout_value,
            status="error",
        )


def cleanup_linux_sandbox() -> None:
    """Terminate the current request's E2B sandbox, if one was created."""
    request_context = get_tool_request_context()
    sandbox = request_context.pop(_SANDBOX_CONTEXT_KEY, None)
    request_context.pop(_SANDBOX_CALL_COUNT_KEY, None)
    release_user_lock = sandbox is None
    try:
        if sandbox is not None:
            try:
                sandbox.kill()
                release_user_lock = True
            except Exception as exc:
                logging.warning("Failed to kill E2B sandbox: %s", exc)
    finally:
        if release_user_lock:
            _release_user_sandbox(request_context)


__all__ = ["cleanup_linux_sandbox", "linux_sandbox_tool"]
