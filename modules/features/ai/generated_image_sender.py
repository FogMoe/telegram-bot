import logging
from pathlib import Path
from typing import Any

from core.telegram_utils import retry_telegram_send, telegram_error_summary

from .tools.image_tools import pop_generated_image_file

MAX_GENERATED_IMAGES_PER_REPLY = 10


def _iter_generate_image_results(tool_logs: list[dict]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for tool_log in tool_logs:
        if tool_log.get("type") != "tool_result":
            continue
        if tool_log.get("tool_name") != "generate_image":
            continue
        result = tool_log.get("internal_result") or tool_log.get("result")
        if isinstance(result, dict):
            results.append(result)
    return results


def _summarise_generate_image_result(result: dict[str, Any]) -> dict[str, Any]:
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


def _cleanup_generated_image(path: Path, logger: logging.Logger) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Failed to clean generated image file %s: %s", path, exc)


async def _send_photo_once(
    *,
    bot: Any,
    chat_id: int,
    path: Path,
) -> Any:
    with path.open("rb") as file_obj:
        return await bot.send_photo(
            chat_id=chat_id,
            photo=file_obj,
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
            lambda: _send_photo_once(bot=bot, chat_id=chat_id, path=path),
            logger=logger,
            action="send generated image as photo",
        )
    except Exception as exc:
        last_error = exc

    logger.warning(
        "Photo send failed, trying document fallback: %s",
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
            action="send generated image as document",
        )
    except Exception as exc:
        last_error = exc

    logger.warning(
        "Generated image send failed after retry: %s",
        telegram_error_summary(last_error),
    )
    return None


def _collect_generated_images_from_result(
    result: dict[str, Any],
    *,
    limit: int = MAX_GENERATED_IMAGES_PER_REPLY,
) -> list[dict[str, Any]]:
    images: list[dict[str, Any]] = []
    if limit <= 0:
        return images
    if result.get("status") != "generated":
        return images
    result_image = result.get("image")
    if isinstance(result_image, dict):
        return [result_image][:limit]
    result_images = result.get("images")
    if not isinstance(result_images, list):
        return images
    for image in result_images:
        if isinstance(image, dict):
            images.append(image)
    return images[:limit]


def _collect_generated_images(tool_logs: list[dict]) -> list[dict[str, Any]]:
    images: list[dict[str, Any]] = []
    for tool_log in tool_logs:
        if tool_log.get("media_sent"):
            continue
        if tool_log.get("type") != "tool_result":
            continue
        if tool_log.get("tool_name") != "generate_image":
            continue
        result = tool_log.get("internal_result") or tool_log.get("result")
        if not isinstance(result, dict):
            continue
        images.extend(_collect_generated_images_from_result(result))
    return images[:MAX_GENERATED_IMAGES_PER_REPLY]


async def send_generated_images_from_tool_result(
    *,
    bot: Any,
    chat_id: int,
    result: dict[str, Any],
    logger: logging.Logger,
) -> list[Any]:
    """Send generated images referenced by a single image tool result."""
    images = _collect_generated_images_from_result(result)
    if not images:
        logger.info(
            "No generated images to send; generate_image_result=%s",
            _summarise_generate_image_result(result),
        )
        return []

    logger.info("Preparing to send %s generated image(s) to chat_id=%s", len(images), chat_id)

    sent_messages: list[Any] = []
    for image in images:
        image_id = str(image.get("image_id") or "").strip()
        file_path = pop_generated_image_file(image_id)
        if not file_path:
            logger.warning("Generated image file reference is missing for image_id=%s", image_id)
            continue

        path = Path(str(file_path))
        if not path.exists() or not path.is_file():
            logger.warning("Generated image file does not exist: %s", path)
            continue

        logger.info(
            "Sending generated image image_id=%s chat_id=%s mime_type=%s size_bytes=%s",
            image_id,
            chat_id,
            image.get("mime_type"),
            image.get("size_bytes"),
        )
        try:
            sent_message = await _send_with_retry(
                bot=bot,
                chat_id=chat_id,
                path=path,
                filename=str(image.get("filename") or path.name),
                logger=logger,
            )
            if sent_message is not None:
                sent_messages.append(sent_message)
                logger.info(
                    "Generated image sent image_id=%s chat_id=%s telegram_message_id=%s",
                    image_id,
                    chat_id,
                    getattr(sent_message, "message_id", None),
                )
            else:
                logger.warning("Generated image was not sent image_id=%s chat_id=%s", image_id, chat_id)
        finally:
            _cleanup_generated_image(path, logger)

    logger.info(
        "Generated image sending finished chat_id=%s sent=%s requested=%s",
        chat_id,
        len(sent_messages),
        len(images),
    )
    return sent_messages


def _limit_generated_image_result(
    result: dict[str, Any],
    *,
    limit: int,
) -> dict[str, Any] | None:
    images = _collect_generated_images_from_result(result, limit=limit)
    if not images:
        return None
    limited_result = dict(result)
    limited_result.pop("images", None)
    limited_result["image"] = images[0]
    limited_result["count"] = len(images)
    return limited_result


def _unsent_image_tool_results(tool_logs: list[dict]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for result in _iter_generate_image_results(tool_logs):
        results.append(result)
    return results


async def send_generated_images_from_tool_logs(
    *,
    bot: Any,
    chat_id: int,
    tool_logs: list[dict],
    logger: logging.Logger,
) -> list[Any]:
    """Send generated images referenced by image generation tool results."""
    image_tool_results = _unsent_image_tool_results(
        [
            tool_log
            for tool_log in tool_logs
            if not tool_log.get("media_sent")
        ]
    )
    images = _collect_generated_images(tool_logs)
    if not images:
        if image_tool_results:
            logger.info(
                "No generated images to send; generate_image_results=%s",
                [_summarise_generate_image_result(result) for result in image_tool_results],
            )
        return []

    sent_messages: list[Any] = []
    remaining = MAX_GENERATED_IMAGES_PER_REPLY
    for result in image_tool_results:
        limited_result = _limit_generated_image_result(result, limit=remaining)
        if limited_result is None:
            continue
        sent_messages.extend(
            await send_generated_images_from_tool_result(
                bot=bot,
                chat_id=chat_id,
                result=limited_result,
                logger=logger,
            )
        )
        remaining -= len(_collect_generated_images_from_result(limited_result))
        if remaining <= 0:
            break
    return sent_messages


__all__ = [
    "send_generated_images_from_tool_logs",
    "send_generated_images_from_tool_result",
]
