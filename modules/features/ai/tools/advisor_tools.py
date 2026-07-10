import logging
import math
import threading
import time
from typing import Any

from core import config

from ..provider_resolver import get_models_for_task, get_provider_order_for_task
from ..task_runner import run_ai_task
from .context import get_tool_request_context

_CALL_COUNT_CONTEXT_KEY = "_advisor_call_count"
_RATE_LIMITS: dict[str, list[float]] = {}
_RATE_LIMIT_LOCK = threading.Lock()
_ADVISOR_SEMAPHORE = threading.BoundedSemaphore(
    config.AI_ADVISOR_MAX_CONCURRENT_REQUESTS
)


def _configured_advisor_available() -> bool:
    providers = get_provider_order_for_task("advisor")
    for provider in providers:
        try:
            if get_models_for_task(provider, "advisor"):
                return True
        except Exception as exc:
            logging.warning(
                "Advisor skipped invalid provider=%s during configuration check: %s",
                provider,
                exc,
            )
    return False


def _reserve_request_call(context: dict[str, object]) -> bool:
    max_calls = config.AI_ADVISOR_MAX_CALLS_PER_REQUEST
    current_calls = int(context.get(_CALL_COUNT_CONTEXT_KEY, 0) or 0)
    if current_calls >= max_calls:
        return False
    context[_CALL_COUNT_CONTEXT_KEY] = current_calls + 1
    return True


def _reserve_user_rate_limit(user_id: object) -> tuple[bool, int | None]:
    now = time.monotonic()
    window_seconds = config.AI_ADVISOR_RATE_LIMIT_WINDOW_SECONDS
    max_calls = config.AI_ADVISOR_RATE_LIMIT_MAX_CALLS
    cutoff = now - window_seconds
    user_key = str(user_id)

    with _RATE_LIMIT_LOCK:
        for existing_user_key, existing_timestamps in list(_RATE_LIMITS.items()):
            active_timestamps = [
                timestamp
                for timestamp in existing_timestamps
                if timestamp > cutoff
            ]
            if active_timestamps:
                _RATE_LIMITS[existing_user_key] = active_timestamps
            else:
                _RATE_LIMITS.pop(existing_user_key, None)

        timestamps = list(_RATE_LIMITS.get(user_key, []))
        if len(timestamps) >= max_calls:
            _RATE_LIMITS[user_key] = timestamps
            retry_after = math.ceil(max(1, window_seconds - (now - timestamps[0])))
            return False, retry_after

        timestamps.append(now)
        _RATE_LIMITS[user_key] = timestamps
        return True, None


def _advisor_messages(task: str, case_facts: str | None) -> list[dict[str, str]]:
    user_content = f"Task:\n{task.strip()}"
    if case_facts and case_facts.strip():
        user_content += f"\n\nCase facts:\n{case_facts.strip()}"
    return [
        {"role": "system", "content": config.ADVISOR_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def _response_text(response: Any) -> str:
    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError, TypeError) as exc:
        raise RuntimeError("Advisor returned an invalid response") from exc
    return str(content or "").strip()


def _usage_total_tokens(response: Any) -> object:
    usage = getattr(response, "usage", None)
    if isinstance(usage, dict):
        return usage.get("total_tokens")
    return getattr(usage, "total_tokens", None)


def advisor_tool(task: str, case_facts: str | None = None) -> dict[str, Any]:
    if not task or not task.strip():
        return {
            "status": "error",
            "error": "Advisor task must not be empty.",
        }

    request_context = get_tool_request_context()
    user_id = request_context.get("user_id") if request_context else None
    if user_id is None:
        return {
            "status": "unavailable",
            "error": "The reasoning advisor requires an active user request.",
        }

    if not _reserve_request_call(request_context):
        return {
            "status": "blocked",
            "error": "Advisor call limit reached for this request.",
            "blocked_reason": "call_limit",
        }

    if not _configured_advisor_available():
        return {
            "status": "unavailable",
            "error": "The reasoning advisor is not configured. Continue without it.",
        }

    if not _ADVISOR_SEMAPHORE.acquire(blocking=False):
        return {
            "status": "busy",
            "error": "The reasoning advisor is busy. Continue without it.",
        }

    try:
        allowed, retry_after = _reserve_user_rate_limit(user_id)
        if not allowed:
            return {
                "status": "blocked",
                "error": "The reasoning advisor rate limit has been reached.",
                "blocked_reason": "user_rate_limit",
                "retry_after_seconds": retry_after,
            }

        started_at = time.monotonic()
        response = run_ai_task(
            "advisor",
            messages=_advisor_messages(task.strip(), case_facts),
            max_tokens=config.AI_ADVISOR_MAX_OUTPUT_TOKENS,
            timeout=config.AI_ADVISOR_TIMEOUT_SECONDS,
        )
        advice = _response_text(response)
        if not advice:
            logging.warning("Advisor returned an empty response")
            return {
                "status": "error",
                "error": "The reasoning advisor returned no advice. Continue without it.",
            }

        logging.info(
            "Advisor completed model=%s elapsed=%.2fs total_tokens=%s",
            getattr(response, "model", None),
            time.monotonic() - started_at,
            _usage_total_tokens(response),
        )
        return {"status": "ok", "advice": advice}
    except Exception as exc:
        logging.exception("Advisor failed: %s", exc)
        return {
            "status": "error",
            "error": "The reasoning advisor is temporarily unavailable. Continue without it.",
        }
    finally:
        _ADVISOR_SEMAPHORE.release()
