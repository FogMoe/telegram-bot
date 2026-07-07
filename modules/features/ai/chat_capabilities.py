from fnmatch import fnmatchcase

from core import config

from .provider_resolver import provider_model_for_task


def _normalize_model_name(model: str | None) -> str:
    return (model or "").strip().lower()


def chat_model_for_service(service_name: str) -> str | None:
    try:
        return provider_model_for_task(service_name, "chat")
    except RuntimeError:
        return None


def chat_model_supports_vision(model: str | None) -> bool:
    normalized_model = _normalize_model_name(model)
    if not normalized_model:
        return True

    for pattern in config.AI_CHAT_TEXT_ONLY_MODELS:
        normalized_pattern = _normalize_model_name(pattern)
        if normalized_pattern and fnmatchcase(normalized_model, normalized_pattern):
            return False
    return True


def chat_service_supports_vision(service_name: str) -> bool:
    return chat_model_supports_vision(chat_model_for_service(service_name))
