"""Background conversation summarization using LiteLLM providers."""

import asyncio
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Tuple

from core import config, mysql_connection
from core.token_estimator import estimate_tokens

from .provider_resolver import (
    completion_kwargs_for_task,
    get_models_for_task,
    get_provider_order_for_task,
)
from .tool_runner import run_tool_loop
from .tools.context import clear_tool_request_context, set_tool_request_context
from .tools.schemas import SUMMARY_SEARCH_PRIOR_CONTEXT_TOOL
from .tools.summary_tools import search_prior_context_tool

SUMMARY_MAX_TOKENS = 2500
SUMMARY_RETRY_LIMIT = 3
SUMMARY_TOOL_MAX_ITERATIONS = 4
SUMMARY_TOOLS = [SUMMARY_SEARCH_PRIOR_CONTEXT_TOOL]
SUMMARY_TOOL_HANDLERS = {"search_prior_context": search_prior_context_tool}

_SUMMARY_EXECUTOR = ThreadPoolExecutor(max_workers=2)


def schedule_summary_generation(user_id: int) -> None:
    """Submit a background task to summarize the latest permanent snapshot."""

    if user_id is None:
        return
    _SUMMARY_EXECUTOR.submit(_process_summary_for_user, user_id)


def _generate_and_store_summary(user_id: int) -> Optional[str]:
    record = _fetch_pending_snapshot(user_id)
    if not record:
        return None

    record_id, snapshot_text = record
    previous_summary = _fetch_previous_summary(user_id, record_id)
    summary_text = _generate_summary(
        user_id,
        record_id,
        snapshot_text,
        previous_summary,
    )
    if summary_text is None:
        logging.warning("Conversation summary generation failed for user %s after retries.", user_id)
        return None

    _store_summary(record_id, summary_text)
    return summary_text


async def generate_summary_immediately(user_id: int) -> Optional[str]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _SUMMARY_EXECUTOR,
        _generate_and_store_summary,
        user_id,
    )


def _process_summary_for_user(user_id: int) -> None:
    try:
        summary_text = _generate_and_store_summary(user_id)
        if summary_text is None:
            return
        mysql_connection.run_sync(
            mysql_connection.async_update_latest_history_state_summary(
                user_id,
                summary_text,
            )
        )
    except Exception as exc:  # pragma: no cover - defensive logging
        logging.exception("Unexpected error while processing summary for user %s: %s", user_id, exc)


def _fetch_pending_snapshot(user_id: int) -> Optional[Tuple[int, str]]:
    row = mysql_connection.run_sync(
        mysql_connection.fetch_one(
            "SELECT id, conversation_snapshot FROM permanent_chat_records "
            "WHERE user_id = %s AND (summary IS NULL OR summary = '') "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (user_id,),
        )
    )
    if not row:
        return None

    snapshot = row[1]
    if isinstance(snapshot, bytes):
        snapshot = snapshot.decode("utf-8")
    elif not isinstance(snapshot, str):
        snapshot = json.dumps(snapshot, ensure_ascii=False)

    return row[0], snapshot


def _fetch_previous_summary(user_id: int, record_id: int) -> str:
    row = mysql_connection.run_sync(
        mysql_connection.fetch_one(
            "SELECT summary FROM permanent_chat_records "
            "WHERE user_id = %s AND id < %s "
            "AND summary IS NOT NULL AND summary <> '' "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (user_id, record_id),
        )
    )
    if not row or row[0] is None:
        return ""
    value = row[0]
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="ignore")
    previous_summary = str(value).strip()
    return "" if previous_summary == "暂无摘要" else previous_summary


def _format_history_for_summary(snapshot_text: str) -> str:
    def _xml_unescape(value: str) -> str:
        return (
            value.replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&quot;", '"')
            .replace("&apos;", "'")
            .replace("&amp;", "&")
        )

    def _flatten_text(value: str) -> str:
        return re.sub(r"\s+", " ", value).strip()

    def _extract_metadata_attrs(content: str) -> dict[str, str]:
        match = re.search(r"<metadata\s+([^>]*)>", content)
        if not match:
            return {}
        attrs_text = match.group(1)
        attrs = {}
        for key, value in re.findall(r'(\w+)="(.*?)"', attrs_text):
            attrs[key] = _flatten_text(_xml_unescape(value))
        return attrs

    def _find_tag(content: str, tag: str) -> str:
        match = re.search(fr"<{tag}>(.*?)</{tag}>", content, re.DOTALL)
        if not match:
            return ""
        return _flatten_text(_xml_unescape(match.group(1)))

    def _format_runtime_event(content: str) -> str | None:
        attrs = _extract_metadata_attrs(content)
        event_type = attrs.get("type")
        if event_type not in {"bot_event", "user_event"}:
            return None

        attr_order = (
            "chat_type",
            "title",
            "timestamp",
            "user",
            "origin",
            "event",
            "command",
            "cause",
            "content_type",
            "redacted",
            "message_id",
            "reply_to_message_id",
        )
        parts = [
            f"{key}={attrs[key]}"
            for key in attr_order
            if attrs.get(key)
        ]
        if event_type == "bot_event":
            displayed_message = _find_tag(content, "displayed_message")
            if displayed_message:
                parts.append(f"displayed_message={displayed_message}")
            label = "BOT_EVENT"
        else:
            callback_match = re.search(r"<callback\s+([^>]*)/>", content)
            if callback_match:
                for key, value in re.findall(r'(\w+)="(.*?)"', callback_match.group(1)):
                    parts.append(f"callback_{key}={_flatten_text(_xml_unescape(value))}")
            label = "USER_ACTION"
        return f"{label}: " + " | ".join(parts)

    def _extract_scheduled_task_fields(content: str) -> dict | None:
        if 'origin="scheduled_task"' not in content:
            return None

        def _find(tag: str) -> str:
            match = re.search(fr"<{tag}>(.*?)</{tag}>", content, re.DOTALL)
            if not match:
                return ""
            return _flatten_text(_xml_unescape(match.group(1)))

        return {
            "attrs": _extract_metadata_attrs(content),
            "trigger": _find("trigger"),
            "context": _find("context"),
            "instruction": _find("instruction"),
        }

    try:
        messages = json.loads(snapshot_text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return snapshot_text

    if not isinstance(messages, list):
        return snapshot_text

    lines: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        content = message.get("content") or ""
        if isinstance(content, str) and 'origin="history_state"' in content:
            continue

        if role == "user":
            if content:
                if isinstance(content, str):
                    if 'origin="idle_recap"' in content:
                        attrs = _extract_metadata_attrs(content)
                        timestamp = attrs.get("timestamp")
                        line = "IDLE_FOLLOWUP_TRIGGER"
                        if timestamp:
                            line = f"{line}: timestamp={timestamp}"
                        lines.append(line)
                        continue
                    runtime_event_line = _format_runtime_event(content)
                    if runtime_event_line is not None:
                        lines.append(runtime_event_line)
                        continue
                    scheduled_fields = _extract_scheduled_task_fields(content)
                    if scheduled_fields is not None:
                        attrs = scheduled_fields.get("attrs") or {}
                        parts = []
                        attr_order = (
                            "type",
                            "timestamp",
                            "user",
                            "origin",
                            "scheduled_at",
                            "scheduled_for",
                        )
                        for key in attr_order:
                            value = attrs.get(key)
                            if value:
                                parts.append(f"{key}={value}")
                        for key in sorted(k for k in attrs.keys() if k not in attr_order):
                            value = attrs.get(key)
                            if value:
                                parts.append(f"{key}={value}")
                        if scheduled_fields.get("trigger"):
                            parts.append(f"trigger={scheduled_fields['trigger']}")
                        if scheduled_fields.get("context"):
                            parts.append(f"context={scheduled_fields['context']}")
                        if scheduled_fields.get("instruction"):
                            parts.append(f"instruction={scheduled_fields['instruction']}")
                        line = "SCHEDULED_TRIGGER"
                        if parts:
                            line = f"{line}: " + " | ".join(parts)
                        lines.append(line)
                        continue
                lines.append(f"USER: {content}")
            continue

        if role == "assistant":
            if content:
                lines.append(f"ASSISTANT: {content}")
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                function_payload = call.get("function") or {}
                tool_name = function_payload.get("name") or "tool"
                arguments = function_payload.get("arguments") or ""
                lines.append(f"TOOL_CALL[{tool_name}]: {arguments}")
            continue

        if role == "tool":
            tool_name = message.get("name") or "tool"
            tool_content = message.get("content") or ""
            lines.append(f"TOOL_RETURN[{tool_name}]: {tool_content}")
            continue

        if content:
            lines.append(f"{role or 'MESSAGE'}: {content}")

    return "\n\n".join(lines)


def _trim_summary_to_tokens(
    summary: str,
    max_tokens: int,
    *,
    model: str | None = None,
) -> str:
    if not summary:
        return summary

    if estimate_tokens(summary, guard_ratio=1.0, model=model) <= max_tokens:
        return summary

    low, high = 0, len(summary)
    while low < high:
        mid = (low + high) // 2
        candidate = summary[:mid]
        if estimate_tokens(candidate, guard_ratio=1.0, model=model) <= max_tokens:
            low = mid + 1
        else:
            high = mid

    return summary[: max(low - 1, 0)].rstrip()


def _run_summary_agent(
    messages: list[dict],
    user_id: int,
    record_id: int,
) -> tuple[str, str]:
    last_error: Exception | None = None
    tool_context = {
        "user_id": user_id,
        "summary_record_id": record_id,
    }
    set_tool_request_context(tool_context)
    try:
        for provider in get_provider_order_for_task("summary"):
            try:
                models = get_models_for_task(provider, "summary")
            except Exception as exc:
                logging.warning(
                    "Summary skipped invalid provider=%s: %s",
                    provider,
                    exc,
                )
                last_error = exc
                continue

            for model in models:
                try:
                    content, _tool_logs = run_tool_loop(
                        provider,
                        model,
                        messages,
                        tool_context,
                        provider_name="Summary",
                        max_tokens=SUMMARY_MAX_TOKENS,
                        max_iterations=SUMMARY_TOOL_MAX_ITERATIONS,
                        completion_kwargs=completion_kwargs_for_task(
                            provider,
                            "summary",
                        ),
                        tool_definitions=SUMMARY_TOOLS,
                        tool_handlers=SUMMARY_TOOL_HANDLERS,
                        system_prompt_override=config.SUMMARY_SYSTEM_PROMPT,
                    )
                    summary_text = str(content or "").strip()
                    if not summary_text:
                        raise ValueError("summary model returned empty content")
                    return summary_text, str(model)
                except Exception as exc:
                    logging.warning(
                        "Summary failed via provider=%s model=%s: %s",
                        provider,
                        model,
                        exc,
                    )
                    last_error = exc
    finally:
        clear_tool_request_context()

    raise RuntimeError("All providers failed for summary generation") from last_error


def _generate_summary(
    user_id: int,
    record_id: int,
    snapshot_text: str,
    previous_summary: str,
) -> Optional[str]:
    transcript = _format_history_for_summary(snapshot_text)
    prompt = (
        "请按照系统要求总结 CURRENT_TRANSCRIPT。PREVIOUS_SUMMARY 是上一份"
        "有效归档摘要，只用于理解跨段连续性。\n\n"
        f"PREVIOUS_SUMMARY:\n{previous_summary or '无'}\n\n"
        f"CURRENT_TRANSCRIPT:\n{transcript}"
    )
    messages = [{"role": "user", "content": prompt}]

    for attempt in range(1, SUMMARY_RETRY_LIMIT + 1):
        try:
            summary, response_model = _run_summary_agent(
                messages,
                user_id,
                record_id,
            )
            if summary:
                summary = _trim_summary_to_tokens(
                    summary,
                    SUMMARY_MAX_TOKENS,
                    model=response_model,
                )
                if attempt > 1:
                    logging.info(
                        "Summary generated successfully for user %s (attempt %s)",
                        user_id,
                        attempt,
                    )
                return summary
        except Exception as exc:  # pragma: no cover - defensive logging
            logging.warning(
                "Attempt %s/%s to summarize user %s failed: %s",
                attempt,
                SUMMARY_RETRY_LIMIT,
                user_id,
                exc,
            )

    return None


def _store_summary(record_id: int, summary_text: str) -> None:
    mysql_connection.run_sync(
        mysql_connection.execute(
            "UPDATE permanent_chat_records SET summary = %s WHERE id = %s",
            (summary_text, record_id),
        )
    )
