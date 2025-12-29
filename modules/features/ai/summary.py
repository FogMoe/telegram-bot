"""Background conversation summarization using Gemini."""

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Tuple

from google import genai
from google.genai import types

from core import config, mysql_connection

APIKEY=config.GEMINI_API_KEY

SUMMARY_MODEL = "gemini-flash-latest"
SUMMARY_FALLBACK_MODEL = "gemini-flash-lite-latest"  # 失败重试时使用的降级模型
SUMMARY_MAX_CHARS = 8000
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
    connection = mysql_connection.create_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT id, conversation_snapshot FROM permanent_chat_records "
            "WHERE user_id = %s AND (summary IS NULL OR summary = '') "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (user_id,),
        )
        row = cursor.fetchone()
        if not row:
            return None

        snapshot = row[1]
        if isinstance(snapshot, bytes):
            snapshot = snapshot.decode("utf-8")
        elif not isinstance(snapshot, str):
            snapshot = json.dumps(snapshot, ensure_ascii=False)

        return row[0], snapshot
    finally:
        cursor.close()
        connection.close()


def _generate_summary(user_id: int, snapshot_text: str) -> Optional[str]:
    client = genai.Client(api_key=APIKEY)

    prompt = (
        "你是一名聊天记录整理助手。接下来是一段雾萌娘与用户的完整对话历史（JSON列表格式）。"
        "请提炼要点，提供一份概述，控制在8000个字符以内，可以分段或列举重点。"
        "请覆盖：对话背景、重要事件或需求、情绪氛围、需要跟进的事项。"
        "如果内容无有效对话，请返回\"暂无摘要\"。\n\n"
        f"对话内容：\n{snapshot_text}"
    )

    contents = [
        types.Content(role="user", parts=[types.Part.from_text(text=prompt)])
    ]

    config = types.GenerateContentConfig(
        system_instruction=SUMMARY_SYSTEM_PROMPT,
        temperature=0.2,
        max_output_tokens=7680,
    )

    # 首先尝试使用主模型
    for attempt in range(1, SUMMARY_RETRY_LIMIT + 1):
        model_to_use = SUMMARY_MODEL if attempt == 1 else SUMMARY_FALLBACK_MODEL
        try:
            response = client.models.generate_content(
                model=model_to_use,
                contents=contents,
                config=config,
            )
            summary = (response.text or "").strip()
            if summary:
                if len(summary) > SUMMARY_MAX_CHARS:
                    summary = summary[:SUMMARY_MAX_CHARS]
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
    connection = mysql_connection.create_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "UPDATE permanent_chat_records SET summary = %s WHERE id = %s",
            (summary_text, record_id),
        )
        connection.commit()
    finally:
        cursor.close()
        connection.close()
