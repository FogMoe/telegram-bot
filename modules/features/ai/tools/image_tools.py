import base64
import json
import logging
import math
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import requests

from core import config
from .context import get_tool_request_context

logger = logging.getLogger(__name__)

DEFAULT_WIDTH = 1024
DEFAULT_HEIGHT = 1024
DEFAULT_STEPS = 9
MAX_PROMPT_CHARS = 2000
MAX_IMAGE_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_IMAGE_BYTES = 16 * 1024 * 1024
GENERATED_IMAGE_TTL_SECONDS = 60 * 60
GENERATED_IMAGE_FILE_PREFIX = "ai_generated_"
GENERATED_IMAGE_DIR = config.BASE_DIR / "logs" / "generated_images"
IMAGE_RATE_LIMIT_WINDOW_SECONDS = 5 * 60
IMAGE_RATE_LIMIT_MAX_GENERATIONS = 2

_SESSION_LOCAL = threading.local()
_GENERATED_IMAGE_FILES: dict[str, str] = {}
_GENERATED_IMAGE_LOCK = threading.Lock()
_IMAGE_RATE_LIMITS: dict[int, list[float]] = {}
_IMAGE_RATE_LIMIT_LOCK = threading.Lock()
_IMAGE_VALUE_KEYS = {
    "b64_json",
    "base64",
    "image_base64",
    "image",
    "content",
}
_IMAGE_CONTAINER_KEYS = (
    "items",
    "images",
    "data",
    "results",
    "outputs",
    "output",
)


class ImageGenerationSizeError(ValueError):
    pass


def _get_request_user_id() -> Optional[int]:
    context = get_tool_request_context()
    user_id = context.get("user_id")
    try:
        return int(user_id)
    except (TypeError, ValueError):
        return None


def _prune_image_rate_limits(now: float) -> None:
    cutoff = now - IMAGE_RATE_LIMIT_WINDOW_SECONDS
    for user_id, timestamps in list(_IMAGE_RATE_LIMITS.items()):
        active_timestamps = [timestamp for timestamp in timestamps if timestamp > cutoff]
        if active_timestamps:
            _IMAGE_RATE_LIMITS[user_id] = active_timestamps
        else:
            _IMAGE_RATE_LIMITS.pop(user_id, None)


def _reserve_image_generation(user_id: int) -> tuple[bool, Optional[float], Optional[int]]:
    now = time.time()
    cutoff = now - IMAGE_RATE_LIMIT_WINDOW_SECONDS

    with _IMAGE_RATE_LIMIT_LOCK:
        _prune_image_rate_limits(now)
        timestamps = [
            timestamp
            for timestamp in _IMAGE_RATE_LIMITS.get(user_id, [])
            if timestamp > cutoff
        ]

        if len(timestamps) >= IMAGE_RATE_LIMIT_MAX_GENERATIONS:
            retry_after = math.ceil(
                max(1, IMAGE_RATE_LIMIT_WINDOW_SECONDS - (now - timestamps[0]))
            )
            _IMAGE_RATE_LIMITS[user_id] = timestamps
            return False, None, retry_after

        timestamps.append(now)
        _IMAGE_RATE_LIMITS[user_id] = timestamps
        return True, now, None


def _release_image_generation(user_id: int, reservation_timestamp: Optional[float]) -> None:
    if reservation_timestamp is None:
        return

    with _IMAGE_RATE_LIMIT_LOCK:
        timestamps = _IMAGE_RATE_LIMITS.get(user_id)
        if not timestamps:
            return
        for index, timestamp in enumerate(timestamps):
            if timestamp == reservation_timestamp:
                del timestamps[index]
                break
        if timestamps:
            _IMAGE_RATE_LIMITS[user_id] = timestamps
        else:
            _IMAGE_RATE_LIMITS.pop(user_id, None)


def _is_expired_generated_image(path: Path, cutoff: float) -> bool:
    try:
        return path.stat().st_mtime < cutoff
    except FileNotFoundError:
        return True
    except OSError as exc:
        logger.warning("Failed to inspect generated image file %s: %s", path, exc)
        return False


def _unlink_generated_image(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Failed to clean generated image file %s: %s", path, exc)


def _cleanup_expired_generated_images() -> None:
    cutoff = time.time() - GENERATED_IMAGE_TTL_SECONDS
    stale_paths: list[Path] = []
    live_paths: set[str] = set()

    with _GENERATED_IMAGE_LOCK:
        for image_id, file_path in list(_GENERATED_IMAGE_FILES.items()):
            path = Path(file_path)
            if _is_expired_generated_image(path, cutoff):
                _GENERATED_IMAGE_FILES.pop(image_id, None)
                stale_paths.append(path)
            else:
                live_paths.add(str(path))

    for path in stale_paths:
        _unlink_generated_image(path)

    try:
        generated_files = list(GENERATED_IMAGE_DIR.glob(f"{GENERATED_IMAGE_FILE_PREFIX}*"))
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning("Failed to scan generated image directory %s: %s", GENERATED_IMAGE_DIR, exc)
        return

    for path in generated_files:
        if str(path) in live_paths:
            continue
        try:
            is_file = path.is_file()
        except OSError as exc:
            logger.warning("Failed to inspect generated image file %s: %s", path, exc)
            continue
        if not is_file:
            continue
        if _is_expired_generated_image(path, cutoff):
            _unlink_generated_image(path)


def _get_session() -> requests.Session:
    session = getattr(_SESSION_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        _SESSION_LOCAL.session = session
    return session


def _coerce_int(
    value: Any,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        int_value = int(value)
    except (TypeError, ValueError):
        int_value = default
    return max(minimum, min(int_value, maximum))


def _normalise_seed(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _build_request_items(
    *,
    prompt: Optional[str],
    width: Optional[int],
    height: Optional[int],
    steps: Optional[int],
    seed: Optional[int],
) -> list[dict[str, Any]]:
    fallback_width = _coerce_int(
        width,
        default=DEFAULT_WIDTH,
        minimum=64,
        maximum=4096,
    )
    fallback_height = _coerce_int(
        height,
        default=DEFAULT_HEIGHT,
        minimum=64,
        maximum=4096,
    )
    fallback_steps = _coerce_int(
        steps,
        default=DEFAULT_STEPS,
        minimum=1,
        maximum=150,
    )

    prompt_text = str(prompt or "").strip()
    if not prompt_text:
        return []
    if len(prompt_text) > MAX_PROMPT_CHARS:
        prompt_text = prompt_text[:MAX_PROMPT_CHARS]

    base_item: dict[str, Any] = {
        "prompt": prompt_text,
        "width": fallback_width,
        "height": fallback_height,
        "steps": fallback_steps,
    }
    seed_value = _normalise_seed(seed)
    if seed_value is not None:
        base_item["seed"] = seed_value

    return [base_item]


def _strip_data_uri(value: str) -> str:
    return re.sub(r"^data:image/[^;]+;base64,", "", value.strip(), flags=re.I)


def _image_extension(content: bytes) -> tuple[str, str]:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return ".webp", "image/webp"
    return ".png", "image/png"


def _extract_image_values(value: Any) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("data:image/") or len(text) > 128:
            return [text]
        return []

    if isinstance(value, list):
        results: list[str] = []
        for item in value:
            results.extend(_extract_image_values(item))
        return results

    if not isinstance(value, dict):
        return []

    results: list[str] = []
    for key in _IMAGE_VALUE_KEYS:
        item_value = value.get(key)
        if isinstance(item_value, str):
            results.append(item_value)

    for key in _IMAGE_CONTAINER_KEYS:
        item_value = value.get(key)
        if item_value is not None:
            results.extend(_extract_image_values(item_value))

    return results


def _redact_image_payload(value: Any) -> Any:
    if isinstance(value, list):
        return [_redact_image_payload(item) for item in value[:5]]
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item_value in value.items():
            if key in _IMAGE_VALUE_KEYS:
                redacted[key] = "[base64 omitted]"
            else:
                redacted[key] = _redact_image_payload(item_value)
        return redacted
    if isinstance(value, str) and len(value) > 200:
        return value[:200] + "..."
    return value


def _payload_preview(payload: Any) -> str:
    try:
        return json.dumps(_redact_image_payload(payload), ensure_ascii=False, default=str)[:1000]
    except TypeError:
        return str(payload)[:1000]


def _read_limited_response_content(
    response: requests.Response,
    *,
    max_bytes: int = MAX_IMAGE_RESPONSE_BYTES,
) -> bytes:
    content_length = response.headers.get("Content-Length")
    if content_length:
        try:
            declared_size = int(content_length)
        except (TypeError, ValueError):
            declared_size = None
        if declared_size is not None and declared_size > max_bytes:
            raise ImageGenerationSizeError(
                f"Image generation API response exceeds {max_bytes} bytes"
            )

    chunks: list[bytes] = []
    total_size = 0
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        total_size += len(chunk)
        if total_size > max_bytes:
            raise ImageGenerationSizeError(
                f"Image generation API response exceeds {max_bytes} bytes"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _save_image(
    *,
    image_base64: str,
    item: dict[str, Any],
    index: int,
) -> dict[str, Any]:
    stripped_base64 = _strip_data_uri(image_base64)
    estimated_size = (len(stripped_base64) * 3) // 4
    if estimated_size > MAX_IMAGE_BYTES:
        raise ImageGenerationSizeError(
            f"Generated image exceeds {MAX_IMAGE_BYTES} bytes"
        )

    raw = base64.b64decode(stripped_base64, validate=False)
    if len(raw) > MAX_IMAGE_BYTES:
        raise ImageGenerationSizeError(
            f"Generated image exceeds {MAX_IMAGE_BYTES} bytes"
        )
    extension, mime_type = _image_extension(raw)

    GENERATED_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    image_id = uuid.uuid4().hex
    filename = (
        f"{GENERATED_IMAGE_FILE_PREFIX}{int(time.time() * 1000)}_"
        f"{index}_{image_id[:8]}{extension}"
    )
    path = GENERATED_IMAGE_DIR / filename
    path.write_bytes(raw)
    with _GENERATED_IMAGE_LOCK:
        _GENERATED_IMAGE_FILES[image_id] = str(path)

    return {
        "image_id": image_id,
        "index": index,
        "prompt": item.get("prompt"),
        "width": item.get("width"),
        "height": item.get("height"),
        "steps": item.get("steps"),
        "seed": item.get("seed"),
        "mime_type": mime_type,
        "size_bytes": len(raw),
    }


def _request_and_save_generated_image(
    *,
    request_items: list[dict[str, Any]],
    api_url: str,
    api_token: str,
    timeout: int,
) -> dict[str, Any]:
    payload = {"items": request_items}
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }

    try:
        with _get_session().post(
            api_url,
            headers=headers,
            json=payload,
            stream=True,
            timeout=timeout,
        ) as response:
            response_content = _read_limited_response_content(response)
            if response.status_code >= 400:
                detail = response_content.decode("utf-8", errors="replace")[:500]
                return {
                    "error": "Image generation request failed",
                    "status_code": response.status_code,
                    "details": detail,
                }
            response_payload = json.loads(response_content.decode("utf-8"))
    except ImageGenerationSizeError as exc:
        return {"error": str(exc)}
    except requests.RequestException as exc:
        logger.exception("Image generation request failed: %s", exc)
        return {
            "error": f"Failed to contact image generation API: {exc}",
        }
    except ValueError as exc:
        return {"error": f"Image generation API returned invalid JSON: {exc}"}

    image_values = _extract_image_values(response_payload)
    if not image_values:
        return {
            "error": "Image generation API returned no base64 images",
            "response_preview": _payload_preview(response_payload),
        }

    saved_images: list[dict[str, Any]] = []
    save_errors: list[str] = []
    for index, image_base64 in enumerate(image_values[: len(request_items)]):
        try:
            item = request_items[min(index, len(request_items) - 1)]
            saved_images.append(
                _save_image(
                    image_base64=image_base64,
                    item=item,
                    index=index + 1,
                )
            )
        except Exception as exc:
            logger.exception("Failed to save generated image %s: %s", index + 1, exc)
            save_errors.append(str(exc))

    if not saved_images:
        response = {"error": "Generated image data could not be decoded or saved"}
        if save_errors:
            response["details"] = "; ".join(save_errors[:3])
        return response

    result = {
        "status": "generated",
        "count": len(saved_images),
        "images": saved_images,
        "message": "Generated images are ready and will be sent to Telegram.",
    }
    if save_errors:
        result["warnings"] = save_errors[:3]
    return result


def generate_image_tool(
    prompt: Optional[str] = None,
    width: Optional[int] = None,
    height: Optional[int] = None,
    steps: Optional[int] = None,
    seed: Optional[int] = None,
    **kwargs,
) -> dict[str, Any]:
    """Generate one image and return a temporary image reference."""
    _cleanup_expired_generated_images()

    api_url = (getattr(config, "IMAGE_GEN_API_URL", "") or "").strip()
    api_token = (getattr(config, "IMAGE_GEN_API_TOKEN", "") or "").strip()
    timeout = getattr(config, "IMAGE_GEN_TIMEOUT", 30) or 30

    if not api_url:
        return {"error": "Image generation API URL is not configured"}
    if not api_token:
        return {"error": "Image generation API token is not configured"}
    if kwargs.get("items") is not None or kwargs.get("count") is not None:
        return {"error": "generate_image supports exactly one image per tool call"}

    request_items = _build_request_items(
        prompt=prompt,
        width=width,
        height=height,
        steps=steps,
        seed=seed,
    )
    if not request_items:
        return {"error": "At least one image prompt is required"}

    user_id = _get_request_user_id()
    if user_id is None:
        return {
            "error": "Missing user information, cannot generate image",
            "details": "Image generation requires a user id for per-user rate limiting.",
        }

    allowed, reservation_timestamp, retry_after = _reserve_image_generation(user_id)
    if not allowed:
        return {
            "error": "Image generation rate limit exceeded",
            "details": (
                f"Each user can generate up to {IMAGE_RATE_LIMIT_MAX_GENERATIONS} "
                "images every 5 minutes."
            ),
            "retry_after_seconds": retry_after,
        }

    generated = False
    try:
        result = _request_and_save_generated_image(
            request_items=request_items,
            api_url=api_url,
            api_token=api_token,
            timeout=timeout,
        )
        generated = result.get("status") == "generated"
        return result
    finally:
        if not generated:
            _release_image_generation(user_id, reservation_timestamp)


def pop_generated_image_file(image_id: str) -> Optional[str]:
    if not image_id:
        return None
    with _GENERATED_IMAGE_LOCK:
        return _GENERATED_IMAGE_FILES.pop(image_id, None)


__all__ = ["generate_image_tool", "pop_generated_image_file"]
