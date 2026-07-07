import logging
from pathlib import Path
from typing import Any

from core.telegram_utils import retry_telegram_send, telegram_error_summary

from .tools.voice_tools import pop_generated_audio_file

MAX_GENERATED_AUDIO_PER_REPLY = 3


def _iter_generate_voice_results(tool_logs: list[dict]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for tool_log in tool_logs:
        if tool_log.get("type") != "tool_result":
            continue
        if tool_log.get("tool_name") != "generate_voice":
            continue
        result = tool_log.get("internal_result") or tool_log.get("result")
        if isinstance(result, dict):
            results.append(result)
    return results


def _summarise_generate_voice_result(result: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "status": result.get("status"),
        "error": result.get("error"),
        "count": result.get("count"),
        "retry_after_seconds": result.get("retry_after_seconds"),
    }
    if result.get("details"):
        summary["details"] = str(result.get("details"))[:500]
    if result.get("response_preview"):
        summary["response_preview"] = str(result.get("response_preview"))[:500]
    return {key: value for key, value in summary.items() if value is not None}


def _cleanup_generated_audio(path: Path, logger: logging.Logger) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Failed to clean generated audio file %s: %s", path, exc)


async def _send_voice_once(
    *,
    bot: Any,
    chat_id: int,
    path: Path,
) -> Any:
    with path.open("rb") as file_obj:
        return await bot.send_voice(
            chat_id=chat_id,
            voice=file_obj,
        )


async def _send_audio_once(
    *,
    bot: Any,
    chat_id: int,
    path: Path,
    filename: str,
) -> Any:
    with path.open("rb") as file_obj:
        return await bot.send_audio(
            chat_id=chat_id,
            audio=file_obj,
            filename=filename,
        )


async def _send_document_once(
    *,
    bot: Any,
    chat_id: int,
    path: Path,
    filename: str,
) -> Any:
    with path.open("rb") as file_obj:
        return await bot.send_document(
            chat_id=chat_id,
            document=file_obj,
            filename=filename,
        )


async def _send_with_retry(
    *,
    bot: Any,
    chat_id: int,
    path: Path,
    filename: str,
    logger: logging.Logger,
) -> Any | None:
    last_error: Exception | None = None

    try:
        return await retry_telegram_send(
            lambda: _send_voice_once(bot=bot, chat_id=chat_id, path=path),
            logger=logger,
            action="send generated audio as voice",
        )
    except Exception as exc:
        last_error = exc

    logger.warning(
        "Voice send failed, trying audio fallback: %s",
        telegram_error_summary(last_error),
    )

    try:
        return await retry_telegram_send(
            lambda: _send_audio_once(
                bot=bot,
                chat_id=chat_id,
                path=path,
                filename=filename,
            ),
            logger=logger,
            action="send generated audio as audio",
        )
    except Exception as exc:
        last_error = exc

    logger.warning(
        "Audio send failed, trying document fallback: %s",
        telegram_error_summary(last_error),
    )

    try:
        return await retry_telegram_send(
            lambda: _send_document_once(
                bot=bot,
                chat_id=chat_id,
                path=path,
                filename=filename,
            ),
            logger=logger,
            action="send generated audio as document",
        )
    except Exception as exc:
        last_error = exc

    logger.warning(
        "Generated audio send failed after retry: %s",
        telegram_error_summary(last_error),
    )
    return None


def _collect_generated_audio_from_result(
    result: dict[str, Any],
    *,
    limit: int = MAX_GENERATED_AUDIO_PER_REPLY,
) -> list[dict[str, Any]]:
    audios: list[dict[str, Any]] = []
    if limit <= 0:
        return audios
    if result.get("status") != "generated":
        return audios
    result_audios = result.get("audios")
    if not isinstance(result_audios, list):
        return audios
    for audio in result_audios:
        if isinstance(audio, dict):
            audios.append(audio)
    return audios[:limit]


def _collect_generated_audio(tool_logs: list[dict]) -> list[dict[str, Any]]:
    audios: list[dict[str, Any]] = []
    for tool_log in tool_logs:
        if tool_log.get("media_sent"):
            continue
        if tool_log.get("type") != "tool_result":
            continue
        if tool_log.get("tool_name") != "generate_voice":
            continue
        result = tool_log.get("internal_result") or tool_log.get("result")
        if not isinstance(result, dict):
            continue
        audios.extend(_collect_generated_audio_from_result(result))
    return audios[:MAX_GENERATED_AUDIO_PER_REPLY]


async def send_generated_audio_from_tool_result(
    *,
    bot: Any,
    chat_id: int,
    result: dict[str, Any],
    logger: logging.Logger,
) -> list[Any]:
    """Send generated audio referenced by a single voice tool result."""
    audios = _collect_generated_audio_from_result(result)
    if not audios:
        logger.info(
            "No generated audio to send; generate_voice_result=%s",
            _summarise_generate_voice_result(result),
        )
        return []

    logger.info("Preparing to send %s generated audio clip(s) to chat_id=%s", len(audios), chat_id)

    sent_messages: list[Any] = []
    for audio in audios:
        audio_id = str(audio.get("audio_id") or "").strip()
        file_path = pop_generated_audio_file(audio_id)
        if not file_path:
            logger.warning("Generated audio file reference is missing for audio_id=%s", audio_id)
            continue

        path = Path(str(file_path))
        if not path.exists() or not path.is_file():
            logger.warning("Generated audio file does not exist: %s", path)
            continue

        filename = str(audio.get("filename") or path.name)
        logger.info(
            "Sending generated audio audio_id=%s chat_id=%s mime_type=%s size_bytes=%s",
            audio_id,
            chat_id,
            audio.get("mime_type"),
            audio.get("size_bytes"),
        )
        try:
            sent_message = await _send_with_retry(
                bot=bot,
                chat_id=chat_id,
                path=path,
                filename=filename,
                logger=logger,
            )
            if sent_message is not None:
                sent_messages.append(sent_message)
                logger.info(
                    "Generated audio sent audio_id=%s chat_id=%s telegram_message_id=%s",
                    audio_id,
                    chat_id,
                    getattr(sent_message, "message_id", None),
                )
            else:
                logger.warning("Generated audio was not sent audio_id=%s chat_id=%s", audio_id, chat_id)
        finally:
            _cleanup_generated_audio(path, logger)

    logger.info(
        "Generated audio sending finished chat_id=%s sent=%s requested=%s",
        chat_id,
        len(sent_messages),
        len(audios),
    )
    return sent_messages


def _limit_generated_audio_result(
    result: dict[str, Any],
    *,
    limit: int,
) -> dict[str, Any] | None:
    audios = _collect_generated_audio_from_result(result, limit=limit)
    if not audios:
        return None
    limited_result = dict(result)
    limited_result["audios"] = audios
    limited_result["count"] = len(audios)
    return limited_result


def _unsent_voice_tool_results(tool_logs: list[dict]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for result in _iter_generate_voice_results(tool_logs):
        results.append(result)
    return results


async def send_generated_audio_from_tool_logs(
    *,
    bot: Any,
    chat_id: int,
    tool_logs: list[dict],
    logger: logging.Logger,
) -> list[Any]:
    """Send generated audio referenced by voice generation tool results."""
    voice_tool_results = _unsent_voice_tool_results(
        [
            tool_log
            for tool_log in tool_logs
            if not tool_log.get("media_sent")
        ]
    )
    audios = _collect_generated_audio(tool_logs)
    if not audios:
        if voice_tool_results:
            logger.info(
                "No generated audio to send; generate_voice_results=%s",
                [_summarise_generate_voice_result(result) for result in voice_tool_results],
            )
        return []

    sent_messages: list[Any] = []
    remaining = MAX_GENERATED_AUDIO_PER_REPLY
    for result in voice_tool_results:
        limited_result = _limit_generated_audio_result(result, limit=remaining)
        if limited_result is None:
            continue
        sent_messages.extend(
            await send_generated_audio_from_tool_result(
                bot=bot,
                chat_id=chat_id,
                result=limited_result,
                logger=logger,
            )
        )
        remaining -= len(limited_result.get("audios") or [])
        if remaining <= 0:
            break
    return sent_messages


__all__ = [
    "send_generated_audio_from_tool_logs",
    "send_generated_audio_from_tool_result",
]
