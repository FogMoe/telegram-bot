import asyncio
import logging
from pathlib import Path
from typing import Any

import telegram.error

from .tools.image_tools import pop_generated_image_file

MAX_GENERATED_IMAGES_PER_REPLY = 10


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

    for attempt in range(2):
        try:
            return await _send_photo_once(bot=bot, chat_id=chat_id, path=path)
        except telegram.error.TelegramError as exc:
            last_error = exc
            logger.warning(
                "Failed to send generated image as photo (attempt %s/2): %s",
                attempt + 1,
                exc,
            )
            if attempt == 0:
                await asyncio.sleep(0.5)
        except Exception as exc:
            last_error = exc
            logger.warning(
                "Failed to send generated image as photo (attempt %s/2): %s",
                attempt + 1,
                exc,
            )
            if attempt == 0:
                await asyncio.sleep(0.5)

    logger.warning("Photo send retry failed, trying document fallback: %s", last_error)

    for attempt in range(2):
        try:
            return await _send_document_once(
                bot=bot,
                chat_id=chat_id,
                path=path,
                filename=filename,
            )
        except Exception as exc:
            last_error = exc
            logger.warning(
                "Failed to send generated image as document (attempt %s/2): %s",
                attempt + 1,
                exc,
            )
            if attempt == 0:
                await asyncio.sleep(0.5)

    logger.warning("Generated image send failed after retry: %s", last_error)
    return None


def _collect_generated_images(tool_logs: list[dict]) -> list[dict[str, Any]]:
    images: list[dict[str, Any]] = []
    for tool_log in tool_logs:
        if tool_log.get("type") != "tool_result":
            continue
        if tool_log.get("tool_name") != "generate_image":
            continue
        result = tool_log.get("internal_result") or tool_log.get("result")
        if not isinstance(result, dict) or result.get("status") != "generated":
            continue
        result_images = result.get("images")
        if not isinstance(result_images, list):
            continue
        for image in result_images:
            if isinstance(image, dict):
                images.append(image)
    return images[:MAX_GENERATED_IMAGES_PER_REPLY]


async def send_generated_images_from_tool_logs(
    *,
    bot: Any,
    chat_id: int,
    tool_logs: list[dict],
    logger: logging.Logger,
) -> list[Any]:
    """Send generated images referenced by image generation tool results."""
    images = _collect_generated_images(tool_logs)
    if not images:
        return []

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

        try:
            sent_message = await _send_with_retry(
                bot=bot,
                chat_id=chat_id,
                path=path,
                filename=path.name,
                logger=logger,
            )
            if sent_message is not None:
                sent_messages.append(sent_message)
        finally:
            _cleanup_generated_image(path, logger)

    return sent_messages


__all__ = ["send_generated_images_from_tool_logs"]
