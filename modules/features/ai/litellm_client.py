import logging
from typing import Any, Dict, List

import litellm

from core import config
from core.litellm_models import litellm_model_name, normalize_provider

PROVIDER_SPECIFIC_KEYS = {
    "provider_specific_fields",
}


def _sanitize_tool_call_for_provider(
    tool_call: Dict[str, Any],
    provider: str,
) -> Dict[str, Any]:
    sanitized = dict(tool_call)
    if provider != "gemini":
        for key in PROVIDER_SPECIFIC_KEYS:
            sanitized.pop(key, None)
    else:
        sanitized.pop("id", None)
    return sanitized


def _sanitize_message_for_provider(
    message: Dict[str, Any],
    provider: str,
) -> Dict[str, Any]:
    sanitized = dict(message)
    if provider != "gemini":
        for key in PROVIDER_SPECIFIC_KEYS:
            sanitized.pop(key, None)

    tool_calls = sanitized.get("tool_calls")
    if isinstance(tool_calls, list):
        sanitized["tool_calls"] = [
            _sanitize_tool_call_for_provider(tool_call, provider)
            if isinstance(tool_call, dict)
            else tool_call
            for tool_call in tool_calls
        ]

    if (
        provider == "gemini"
        and sanitized.get("role") == "assistant"
        and sanitized.get("tool_calls")
        and not str(sanitized.get("content") or "").strip()
    ):
        sanitized.pop("content", None)
    if provider == "gemini" and sanitized.get("role") == "tool":
        sanitized.pop("tool_call_id", None)
    return sanitized


def _sanitize_messages_for_provider(
    messages: List[Dict[str, Any]],
    provider: str,
) -> List[Dict[str, Any]]:
    return [
        _sanitize_message_for_provider(message, provider)
        if isinstance(message, dict)
        else message
        for message in messages
    ]


def _azure_api_base() -> str:
    if config.AZURE_OPENAI_API_ENDPOINT:
        return config.AZURE_OPENAI_API_ENDPOINT.rstrip("/")

    base_url = config.AZURE_OPENAI_BASE_URL or ""
    marker = "/openai/deployments/"
    if marker in base_url:
        return base_url.split(marker, 1)[0].rstrip("/")
    return base_url.rstrip("/")


def _openai_compatible_api_base(value: str) -> str:
    base_url = (value or "").rstrip("/")
    suffix = "/chat/completions"
    if base_url.lower().endswith(suffix):
        return base_url[: -len(suffix)].rstrip("/")
    return base_url


def _gemini_native_api_base(value: str) -> str:
    base_url = (value or "").rstrip("/")
    suffix = "/models"
    if base_url.lower().endswith(suffix):
        return base_url[: -len(suffix)].rstrip("/")
    return base_url


def _provider_params(provider: str) -> Dict[str, Any]:
    if provider == "openai":
        api_key = config.OPENAI_API_KEY
        if not api_key and config.OPENAI_BASE_URL:
            api_key = "sk-no-key-required"
        if not api_key:
            raise RuntimeError("Missing OPENAI_API_KEY configuration.")

        params: Dict[str, Any] = {"api_key": api_key}
        if config.OPENAI_BASE_URL:
            params["api_base"] = config.OPENAI_BASE_URL
        return params

    if provider == "gemini":
        if not config.GEMINI_API_KEY:
            raise RuntimeError("Missing GEMINI_API_KEY configuration.")
        if config.GEMINI_OPENAI_COMPATIBLE and not config.GEMINI_API_BASE:
            raise RuntimeError("GEMINI_OPENAI_COMPATIBLE requires GEMINI_API_BASE.")
        params = {"api_key": config.GEMINI_API_KEY}
        if config.GEMINI_API_BASE:
            params["api_base"] = (
                _openai_compatible_api_base(config.GEMINI_API_BASE)
                if config.GEMINI_OPENAI_COMPATIBLE
                else _gemini_native_api_base(config.GEMINI_API_BASE)
            )
        return params

    if provider == "zai":
        if not config.ZAI_API_KEY:
            raise RuntimeError("Missing ZAI_API_KEY configuration.")
        params = {"api_key": config.ZAI_API_KEY}
        if config.ZAI_API_BASE:
            params["api_base"] = config.ZAI_API_BASE
        return params

    if provider == "siliconflow":
        if not config.SILICONFLOW_API_KEY:
            raise RuntimeError("Missing SILICONFLOW_API_KEY configuration.")
        api_base = _openai_compatible_api_base(config.SILICONFLOW_API_BASE)
        if not api_base:
            raise RuntimeError("Missing SILICONFLOW_API_BASE configuration.")
        return {
            "api_key": config.SILICONFLOW_API_KEY,
            "api_base": api_base,
        }

    if provider == "azure":
        if not config.AZURE_OPENAI_API_KEY:
            raise RuntimeError("Missing AZURE_OPENAI_API_KEY configuration.")
        api_base = _azure_api_base()
        if not api_base:
            raise RuntimeError("Missing AZURE_OPENAI_API_ENDPOINT configuration.")
        if not config.AZURE_OPENAI_API_VERSION:
            raise RuntimeError("Missing AZURE_OPENAI_API_VERSION configuration.")
        return {
            "api_key": config.AZURE_OPENAI_API_KEY,
            "api_base": api_base,
            "api_version": config.AZURE_OPENAI_API_VERSION,
        }

    raise RuntimeError(f"Unsupported AI provider: {provider}")


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
