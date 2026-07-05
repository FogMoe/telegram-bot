import json
import logging
import random
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from core import config

logger = logging.getLogger(__name__)

STICKER_CACHE_TTL_SECONDS = 24 * 60 * 60
PACKS_CONFIG_PATH = config.BASE_DIR / "resources" / "ai_sticker_packs.json"

_CACHE_LOCK = threading.Lock()
_STICKER_SET_CACHE: dict[str, dict[str, Any]] = {}


def _load_pack_configs() -> dict[str, dict[str, Any]]:
    if not Path(PACKS_CONFIG_PATH).exists():
        logger.warning("AI sticker pack config does not exist: %s", PACKS_CONFIG_PATH)
        return {}

    try:
        raw_data = json.loads(PACKS_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to load AI sticker pack config: %s", exc)
        return {}

    packs = raw_data.get("packs", [])
    if not isinstance(packs, list):
        return {}

    configs: dict[str, dict[str, Any]] = {}
    for item in packs:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        configs[name] = {
            "name": name,
            "summary": str(item.get("summary") or "").strip(),
            "avoid": str(item.get("avoid") or "").strip(),
        }
    return configs


def _fetch_sticker_set(pack_name: str) -> dict[str, Any]:
    token = config.TELEGRAM_BOT_TOKEN
    if not token:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN configuration.")

    query = urllib.parse.urlencode({"name": pack_name})
    url = f"https://api.telegram.org/bot{token}/getStickerSet?{query}"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "FogMoeBot/ai-sticker-tool"},
    )

    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))

    if not payload.get("ok"):
        description = payload.get("description") or "Telegram getStickerSet failed"
        raise RuntimeError(description)

    result = payload.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("Telegram getStickerSet returned an invalid payload.")
    return result


def _build_metadata(pack_config: dict[str, Any], sticker_set: dict[str, Any]) -> dict[str, Any]:
    emoji_to_file_ids: dict[str, list[str]] = {}
    static_count = 0
    video_count = 0
    animated_count = 0

    stickers = sticker_set.get("stickers") or []
    if not isinstance(stickers, list):
        stickers = []

    for sticker in stickers:
        if not isinstance(sticker, dict):
            continue
        emoji = str(sticker.get("emoji") or "").strip()
        file_id = str(sticker.get("file_id") or "").strip()
        if not emoji or not file_id:
            continue

        emoji_to_file_ids.setdefault(emoji, []).append(file_id)
        if sticker.get("is_video"):
            video_count += 1
        elif sticker.get("is_animated"):
            animated_count += 1
        else:
            static_count += 1

    emoji_counts = {
        emoji: len(file_ids)
        for emoji, file_ids in sorted(
            emoji_to_file_ids.items(),
            key=lambda item: (-len(item[1]), item[0]),
        )
    }
    now = time.time()
    return {
        "name": sticker_set.get("name") or pack_config["name"],
        "title": sticker_set.get("title") or pack_config["name"],
        "summary": pack_config.get("summary") or "",
        "avoid": pack_config.get("avoid") or "",
        "sticker_type": sticker_set.get("sticker_type") or "regular",
        "sticker_count": len(stickers),
        "static_count": static_count,
        "video_count": video_count,
        "animated_count": animated_count,
        "emoji_counts": emoji_counts,
        "emoji_to_file_ids": emoji_to_file_ids,
        "cached_at": now,
        "expires_at": now + STICKER_CACHE_TTL_SECONDS,
    }


def _metadata_for_pack(pack_name: str, *, refresh: bool = False) -> dict[str, Any]:
    pack_configs = _load_pack_configs()
    pack_config = pack_configs.get(pack_name)
    if not pack_config:
        raise ValueError(f"Sticker pack is not configured: {pack_name}")

    now = time.time()
    with _CACHE_LOCK:
        cached = _STICKER_SET_CACHE.get(pack_name)
        if (
            not refresh
            and cached
            and now < float(cached.get("expires_at") or 0)
        ):
            return cached

    sticker_set = _fetch_sticker_set(pack_name)
    metadata = _build_metadata(pack_config, sticker_set)

    with _CACHE_LOCK:
        _STICKER_SET_CACHE[pack_name] = metadata
    return metadata


def _public_pack_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    cached_at = float(metadata.get("cached_at") or 0)
    expires_at = float(metadata.get("expires_at") or 0)
    return {
        "name": metadata.get("name"),
        "title": metadata.get("title"),
        "summary": metadata.get("summary"),
        "avoid": metadata.get("avoid"),
        "sticker_type": metadata.get("sticker_type"),
        "sticker_count": metadata.get("sticker_count"),
        "static_count": metadata.get("static_count"),
        "video_count": metadata.get("video_count"),
        "animated_count": metadata.get("animated_count"),
        "emoji_counts": metadata.get("emoji_counts") or {},
        "cached_at_unix": int(cached_at),
        "expires_at_unix": int(expires_at),
    }


def list_available_stickers_tool(
    pack_name: str | None = None,
) -> dict[str, Any]:
    """Return configured Telegram sticker packs and dynamic emoji choices."""
    pack_configs = _load_pack_configs()
    if not pack_configs:
        return {
            "packs": [],
            "usage": "No sticker packs are configured.",
        }

    if pack_name:
        names = [pack_name.strip()]
    else:
        names = list(pack_configs.keys())

    packs: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for name in names:
        if name not in pack_configs:
            errors.append(
                {
                    "pack_name": name,
                    "error": "Sticker pack is not configured.",
                }
            )
            continue

        try:
            metadata = _metadata_for_pack(name)
            packs.append(_public_pack_metadata(metadata))
        except Exception as exc:
            logger.warning("Failed to load sticker pack %s: %s", name, exc)
            errors.append({"pack_name": name, "error": str(exc)})

    return {
        "packs": packs,
        "errors": errors,
        "cache_ttl_seconds": STICKER_CACHE_TTL_SECONDS,
        "usage": (
            "To send a sticker in the final answer, put a directive on its own line: "
            "[sticker_pack:<pack_name> emoji:<emoji>]. Use only configured pack names "
            "and emoji returned by this tool. Use at most 10 stickers per reply."
        ),
    }


def choose_sticker_file_id(pack_name: str, emoji: str) -> str | None:
    """Choose a random cached sticker file_id for a configured pack and emoji."""
    normalized_pack_name = (pack_name or "").strip()
    normalized_emoji = (emoji or "").strip()
    if not normalized_pack_name or not normalized_emoji:
        return None

    try:
        metadata = _metadata_for_pack(normalized_pack_name)
    except Exception as exc:
        logger.warning(
            "Failed to choose sticker for pack=%s emoji=%s: %s",
            normalized_pack_name,
            normalized_emoji,
            exc,
        )
        return None

    emoji_to_file_ids = metadata.get("emoji_to_file_ids") or {}
    file_ids = emoji_to_file_ids.get(normalized_emoji)
    if not file_ids:
        logger.info(
            "No sticker found for pack=%s emoji=%s",
            normalized_pack_name,
            normalized_emoji,
        )
        return None
    return random.choice(file_ids)
