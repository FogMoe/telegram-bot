from fnmatch import fnmatchcase
from typing import Any

from fogmoe_telegram_bot.core import ai_providers

from .provider_resolver import provider_model_for_task


def _normalize_model_name(model: str | None) -> str:
    return (model or "").strip().lower()


def chat_model_for_service(service_name: str, settings: Any = None) -> str | None:
    try:
        return provider_model_for_task(service_name, "chat", settings)
    except RuntimeError:
        return None


def chat_model_supports_vision(model: str | None, settings: Any = None) -> bool:
    normalized_model = _normalize_model_name(model)
    if not normalized_model:
        return True

    for pattern in ai_providers.read_setting(settings, "AI_CHAT_TEXT_ONLY_MODELS") or ():
        normalized_pattern = _normalize_model_name(pattern)
        if normalized_pattern and fnmatchcase(normalized_model, normalized_pattern):
            return False
    return True


def chat_service_supports_vision(service_name: str, settings: Any = None) -> bool:
    """provider 声明支持视觉，且当前聊天模型不在纯文本模型列表里。"""
    spec = ai_providers.lookup(service_name)
    if spec is not None and not spec.supports("vision"):
        return False
    return chat_model_supports_vision(chat_model_for_service(service_name, settings), settings)
