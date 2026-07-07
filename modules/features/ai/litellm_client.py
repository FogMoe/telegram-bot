import logging
from typing import Any, Dict, List

import litellm

from core import config
from core.litellm_models import litellm_model_name, normalize_provider
from .litellm_message_sanitizer import (
    PROVIDER_SPECIFIC_KEYS,
    sanitize_message_for_provider,
    sanitize_messages_for_provider,
    sanitize_tool_call_for_provider,
)
from .litellm_provider_config import (
    azure_api_base,
    gemini_native_api_base,
    openai_compatible_api_base,
    provider_params,
)


def _sanitize_tool_call_for_provider(
    tool_call: Dict[str, Any],
    provider: str,
) -> Dict[str, Any]:
    return sanitize_tool_call_for_provider(tool_call, provider)


def _sanitize_message_for_provider(
    message: Dict[str, Any],
    provider: str,
) -> Dict[str, Any]:
    return sanitize_message_for_provider(message, provider)


def _sanitize_messages_for_provider(
    messages: List[Dict[str, Any]],
    provider: str,
) -> List[Dict[str, Any]]:
    return sanitize_messages_for_provider(messages, provider)


def _azure_api_base() -> str:
    return azure_api_base()


def _openai_compatible_api_base(value: str) -> str:
    return openai_compatible_api_base(value)


def _gemini_native_api_base(value: str) -> str:
    return gemini_native_api_base(value)


def _provider_params(provider: str) -> Dict[str, Any]:
    return provider_params(provider)


def create_chat_completion(
    provider: str,
    model: str,
    messages: List[Dict[str, Any]],
    **kwargs: Any,
) -> Any:
    litellm_provider = normalize_provider(provider)
    history_provider = (
        "openai"
        if litellm_provider == "gemini" and config.GEMINI_OPENAI_COMPATIBLE
        else litellm_provider
    )
    provider_messages = _sanitize_messages_for_provider(messages, history_provider)
    request_kwargs = {
        key: value
        for key, value in kwargs.items()
        if value is not None
    }
    request_kwargs.setdefault("drop_params", True)

    litellm_model = litellm_model_name(litellm_provider, model)
    logging.debug("Calling LiteLLM provider=%s model=%s", litellm_provider, litellm_model)

    return litellm.completion(
        model=litellm_model,
        messages=provider_messages,
        **_provider_params(litellm_provider),
        **request_kwargs,
    )
