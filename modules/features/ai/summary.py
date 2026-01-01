"""Background conversation summarization using OpenAI-compatible providers."""

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Tuple

from core import config, mysql_connection
from core.token_estimator import estimate_tokens
from .clients import create_gemini_client

SUMMARY_MODEL = config.SUMMARY_MODEL
SUMMARY_FALLBACK_MODEL = config.SUMMARY_FALLBACK_MODEL
SUMMARY_MAX_TOKENS = 2500
SUMMARY_RETRY_LIMIT = 3
SUMMARY_SYSTEM_PROMPT = (
    "你是雾萌娘的对话归档整理员，负责撰写客观、中立的会话摘要。"
    " 请准确提炼对话背景、关键事件或诉求、情绪变化、需要跟进的事项。"
    " 不要捏造信息或过度推测，保持专业、清晰的语气，并控制在合理长度。"
)

_SUMMARY_EXECUTOR = ThreadPoolExecutor(max_workers=2)


def schedule_summary_generation(user_id: int) -> None:
    """Submit a background task to summarize the latest permanent snapshot."""

    if user_id is None:
        return
    _SUMMARY_EXECUTOR.submit(_process_summary_for_user, user_id)


def _process_summary_for_user(user_id: int) -> None:
    try:
        record = _fetch_pending_snapshot(user_id)
        if not record:
            return

        record_id, snapshot_text = record
        summary_text = _generate_summary(user_id, snapshot_text)
        if summary_text is None:
            logging.warning("Conversation summary generation failed for user %s after retries.", user_id)
            return

        _store_summary(record_id, summary_text)
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

        if role == "user":
            if content:
                if isinstance(content, str):
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

        if role == "system":
            content_str = content if isinstance(content, str) else ""
            if 'origin="history_state"' in content_str:
                continue

        if content:
            lines.append(f"{role or 'MESSAGE'}: {content}")

    return "\n\n".join(lines)


def _trim_summary_to_tokens(summary: str, max_tokens: int) -> str:
    if not summary:
        return summary

    if estimate_tokens(summary, guard_ratio=1.0) <= max_tokens:
        return summary

    low, high = 0, len(summary)
    while low < high:
        mid = (low + high) // 2
        candidate = summary[:mid]
        if estimate_tokens(candidate, guard_ratio=1.0) <= max_tokens:
            low = mid + 1
        else:
            high = mid

    return summary[: max(low - 1, 0)].rstrip()


def _generate_summary(user_id: int, snapshot_text: str) -> Optional[str]:
    client = create_gemini_client()

    transcript = _format_history_for_summary(snapshot_text)
    prompt = (
        "你是一名聊天记录整理助手。接下来是一段雾萌娘与用户的对话转录"
        "（包含 USER/ASSISTANT/TOOL 记录）。请提炼要点，提供一份概述，"
        "控制在2500 tokens以内，可以分段或列举重点。"
        "请覆盖：对话背景、重要事件或需求、情绪氛围、需要跟进的事项。"
        "如果内容无有效对话，请返回\"暂无摘要\"。\n\n"
        f"对话内容：\n{transcript}"
    )

    messages = [
        {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]

    for attempt in range(1, SUMMARY_RETRY_LIMIT + 1):
        model_to_use = SUMMARY_MODEL if attempt == 1 else SUMMARY_FALLBACK_MODEL
        try:
            response = client.chat.completions.create(
                model=model_to_use,
                messages=messages,
                temperature=0.2,
                max_tokens=SUMMARY_MAX_TOKENS,
            )
            summary = (response.choices[0].message.content or "").strip()
            if summary:
                summary = _trim_summary_to_tokens(summary, SUMMARY_MAX_TOKENS)
                if attempt > 1:
                    logging.info(
                        "Summary generated successfully using fallback model %s for user %s (attempt %s)",
                        model_to_use,
                        user_id,
                        attempt,
                    )
                return summary
        except Exception as exc:  # pragma: no cover - defensive logging
            logging.warning(
                "Attempt %s/%s to summarize user %s with model %s failed: %s",
                attempt,
                SUMMARY_RETRY_LIMIT,
                user_id,
                model_to_use,
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
