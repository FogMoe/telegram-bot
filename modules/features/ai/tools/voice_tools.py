import logging
import math
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import requests

from core import config
from .context import get_tool_request_context

logger = logging.getLogger(__name__)

FISH_AUDIO_API_URL = "https://api.fish.audio/v1/tts"
DEFAULT_FISH_AUDIO_MODEL = "s2.1-pro-free"
DEFAULT_FISH_AUDIO_REFERENCE_ID = "dc020cb237df4248907565718715b20b"
DEFAULT_AUDIO_FORMAT = "mp3"
DEFAULT_AUDIO_MIME_TYPE = "audio/mpeg"
DEFAULT_VOICE_TIMEOUT_SECONDS = 60
MAX_VOICE_TEXT_CHARS = 500
MAX_AUDIO_RESPONSE_BYTES = 24 * 1024 * 1024
GENERATED_AUDIO_TTL_SECONDS = 60 * 60
GENERATED_AUDIO_FILE_PREFIX = "ai_generated_audio_"
GENERATED_AUDIO_DIR = config.BASE_DIR / "logs" / "generated_audio"
VOICE_RATE_LIMIT_WINDOW_SECONDS = 5 * 60
VOICE_RATE_LIMIT_MAX_GENERATIONS = 3

_SESSION_LOCAL = threading.local()
_GENERATED_AUDIO_FILES: dict[str, str] = {}
_GENERATED_AUDIO_LOCK = threading.Lock()
_VOICE_RATE_LIMITS: dict[int, list[float]] = {}
_VOICE_RATE_LIMIT_LOCK = threading.Lock()


class VoiceGenerationSizeError(ValueError):
    pass


def _get_request_user_id() -> Optional[int]:
    context = get_tool_request_context()
    user_id = context.get("user_id")
    try:
        return int(user_id)
    except (TypeError, ValueError):
        return None


def _prune_voice_rate_limits(now: float) -> None:
    cutoff = now - VOICE_RATE_LIMIT_WINDOW_SECONDS
    for user_id, timestamps in list(_VOICE_RATE_LIMITS.items()):
        active_timestamps = [timestamp for timestamp in timestamps if timestamp > cutoff]
        if active_timestamps:
            _VOICE_RATE_LIMITS[user_id] = active_timestamps
        else:
            _VOICE_RATE_LIMITS.pop(user_id, None)


def _reserve_voice_generation(user_id: int) -> tuple[bool, Optional[float], Optional[int]]:
    now = time.time()
    cutoff = now - VOICE_RATE_LIMIT_WINDOW_SECONDS

    with _VOICE_RATE_LIMIT_LOCK:
        _prune_voice_rate_limits(now)
        timestamps = [
            timestamp
            for timestamp in _VOICE_RATE_LIMITS.get(user_id, [])
            if timestamp > cutoff
        ]

        if len(timestamps) >= VOICE_RATE_LIMIT_MAX_GENERATIONS:
            retry_after = math.ceil(
                max(1, VOICE_RATE_LIMIT_WINDOW_SECONDS - (now - timestamps[0]))
            )
            _VOICE_RATE_LIMITS[user_id] = timestamps
            return False, None, retry_after

        timestamps.append(now)
        _VOICE_RATE_LIMITS[user_id] = timestamps
        return True, now, None


def _release_voice_generation(user_id: int, reservation_timestamp: Optional[float]) -> None:
    if reservation_timestamp is None:
        return

    with _VOICE_RATE_LIMIT_LOCK:
        timestamps = _VOICE_RATE_LIMITS.get(user_id)
        if not timestamps:
            return
        for index, timestamp in enumerate(timestamps):
            if timestamp == reservation_timestamp:
                del timestamps[index]
                break
        if timestamps:
            _VOICE_RATE_LIMITS[user_id] = timestamps
        else:
            _VOICE_RATE_LIMITS.pop(user_id, None)


def _is_expired_generated_audio(path: Path, cutoff: float) -> bool:
    try:
        return path.stat().st_mtime < cutoff
    except FileNotFoundError:
        return True
    except OSError as exc:
        logger.warning("Failed to inspect generated audio file %s: %s", path, exc)
        return False


def _unlink_generated_audio(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Failed to clean generated audio file %s: %s", path, exc)


def _cleanup_expired_generated_audio() -> None:
    cutoff = time.time() - GENERATED_AUDIO_TTL_SECONDS
    stale_paths: list[Path] = []
    live_paths: set[str] = set()

    with _GENERATED_AUDIO_LOCK:
        for audio_id, file_path in list(_GENERATED_AUDIO_FILES.items()):
            path = Path(file_path)
            if _is_expired_generated_audio(path, cutoff):
                _GENERATED_AUDIO_FILES.pop(audio_id, None)
                stale_paths.append(path)
            else:
                live_paths.add(str(path))

    for path in stale_paths:
        _unlink_generated_audio(path)

    try:
        generated_files = list(GENERATED_AUDIO_DIR.glob(f"{GENERATED_AUDIO_FILE_PREFIX}*"))
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning("Failed to scan generated audio directory %s: %s", GENERATED_AUDIO_DIR, exc)
        return

    for path in generated_files:
        if str(path) in live_paths:
            continue
        try:
            is_file = path.is_file()
        except OSError as exc:
            logger.warning("Failed to inspect generated audio file %s: %s", path, exc)
            continue
        if not is_file:
            continue
        if _is_expired_generated_audio(path, cutoff):
            _unlink_generated_audio(path)


def _get_session() -> requests.Session:
    session = getattr(_SESSION_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        _SESSION_LOCAL.session = session
    return session


def _read_limited_response_content(
    response: requests.Response,
    *,
    max_bytes: int = MAX_AUDIO_RESPONSE_BYTES,
) -> bytes:
    content_length = response.headers.get("Content-Length")
    if content_length:
        try:
            declared_size = int(content_length)
        except (TypeError, ValueError):
            declared_size = None
        if declared_size is not None and declared_size > max_bytes:
            raise VoiceGenerationSizeError(
                f"Voice generation API response exceeds {max_bytes} bytes"
            )

    chunks: list[bytes] = []
    total_size = 0
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        total_size += len(chunk)
        if total_size > max_bytes:
            raise VoiceGenerationSizeError(
                f"Voice generation API response exceeds {max_bytes} bytes"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _content_type_to_audio_meta(content_type: str | None) -> tuple[str, str]:
    normalized = (content_type or "").split(";", 1)[0].strip().lower()
    if normalized in {"audio/wav", "audio/wave", "audio/x-wav"}:
        return ".wav", "audio/wav"
    if normalized in {"audio/ogg", "audio/opus"}:
        return ".opus", normalized
    if normalized in {"audio/mpeg", "audio/mp3"}:
        return ".mp3", "audio/mpeg"
    return ".mp3", DEFAULT_AUDIO_MIME_TYPE


def _save_audio(
    *,
    audio_bytes: bytes,
    text: str,
    content_type: str | None,
) -> dict[str, Any]:
    if not audio_bytes:
        raise VoiceGenerationSizeError("Voice generation API returned empty audio")
    if len(audio_bytes) > MAX_AUDIO_RESPONSE_BYTES:
        raise VoiceGenerationSizeError(
            f"Generated audio exceeds {MAX_AUDIO_RESPONSE_BYTES} bytes"
        )

    extension, mime_type = _content_type_to_audio_meta(content_type)
    GENERATED_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    audio_id = uuid.uuid4().hex
    filename = (
        f"{GENERATED_AUDIO_FILE_PREFIX}{int(time.time() * 1000)}_"
        f"{audio_id[:8]}{extension}"
    )
    path = GENERATED_AUDIO_DIR / filename
    path.write_bytes(audio_bytes)
    with _GENERATED_AUDIO_LOCK:
        _GENERATED_AUDIO_FILES[audio_id] = str(path)

    return {
        "audio_id": audio_id,
        "filename": filename,
        "format": DEFAULT_AUDIO_FORMAT,
        "mime_type": mime_type,
        "size_bytes": len(audio_bytes),
        "text_length": len(text),
        "text_preview": text[:120],
    }


def _request_and_save_generated_voice(
    *,
    text: str,
    api_key: str,
    model: str,
    reference_id: str,
    timeout: int,
) -> dict[str, Any]:
    payload = {
        "text": text,
        "reference_id": reference_id,
        "format": DEFAULT_AUDIO_FORMAT,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "model": model,
    }

    try:
        with _get_session().post(
            FISH_AUDIO_API_URL,
            headers=headers,
            json=payload,
            stream=True,
            timeout=timeout,
        ) as response:
            response_content = _read_limited_response_content(response)
            if response.status_code >= 400:
                detail = response_content.decode("utf-8", errors="replace")[:500]
                return {
                    "error": "Voice generation request failed",
                    "status_code": response.status_code,
                    "details": detail,
                }
            audio = _save_audio(
                audio_bytes=response_content,
                text=text,
                content_type=response.headers.get("Content-Type"),
            )
    except VoiceGenerationSizeError as exc:
        return {"error": str(exc)}
    except requests.Timeout as exc:
        logger.warning("Fish Audio request timed out after %s seconds: %s", timeout, exc)
        return {
            "error": f"Fish Audio request timed out after {timeout} seconds",
        }
    except requests.RequestException as exc:
        logger.warning("Fish Audio request failed: %s", exc)
        return {
            "error": f"Failed to contact Fish Audio API: {exc}",
        }
    except Exception as exc:
        logger.exception("Failed to save generated voice: %s", exc)
        return {"error": f"Generated audio could not be saved: {exc}"}

    return {
        "status": "generated",
        "count": 1,
        "audios": [audio],
        "message": "Generated audio is ready and will be sent to Telegram.",
    }


def generate_voice_tool(
    text: Optional[str] = None,
    **kwargs,
) -> dict[str, Any]:
    """Generate one spoken audio clip and return a temporary audio reference."""
    _cleanup_expired_generated_audio()

    if kwargs:
        return {"error": "generate_voice supports only the text argument"}

    speech_text = str(text or "").strip()
    if not speech_text:
        return {"error": "Text is required for voice generation"}
    if len(speech_text) > MAX_VOICE_TEXT_CHARS:
        return {
            "error": (
                f"Voice generation text is too long; max {MAX_VOICE_TEXT_CHARS} characters"
            ),
        }

    api_key = (getattr(config, "FISH_AUDIO_API_KEY", None) or "").strip()
    model = (
        getattr(config, "FISH_AUDIO_MODEL", DEFAULT_FISH_AUDIO_MODEL)
        or DEFAULT_FISH_AUDIO_MODEL
    ).strip()
    reference_id = (
        getattr(config, "FISH_AUDIO_REFERENCE_ID", DEFAULT_FISH_AUDIO_REFERENCE_ID)
        or DEFAULT_FISH_AUDIO_REFERENCE_ID
    ).strip()

    if not api_key:
        return {"error": "Fish Audio API key is not configured"}
    if not model:
        return {"error": "Fish Audio model is not configured"}
    if not reference_id:
        return {"error": "Fish Audio reference voice ID is not configured"}

    user_id = _get_request_user_id()
    if user_id is None:
        return {
            "error": "Missing user information, cannot generate voice",
            "details": "Voice generation requires a user id for per-user rate limiting.",
        }

    allowed, reservation_timestamp, retry_after = _reserve_voice_generation(user_id)
    if not allowed:
        return {
            "error": "Voice generation rate limit exceeded",
            "details": (
                f"Each user can generate up to {VOICE_RATE_LIMIT_MAX_GENERATIONS} "
                "audio clips every 5 minutes."
            ),
            "retry_after_seconds": retry_after,
        }

    generated = False
    try:
        result = _request_and_save_generated_voice(
            text=speech_text,
            api_key=api_key,
            model=model,
            reference_id=reference_id,
            timeout=DEFAULT_VOICE_TIMEOUT_SECONDS,
        )
        generated = result.get("status") == "generated"
        return result
    finally:
        if not generated:
            _release_voice_generation(user_id, reservation_timestamp)


def pop_generated_audio_file(audio_id: str) -> Optional[str]:
    if not audio_id:
        return None
    with _GENERATED_AUDIO_LOCK:
        return _GENERATED_AUDIO_FILES.pop(audio_id, None)


__all__ = ["generate_voice_tool", "pop_generated_audio_file"]
