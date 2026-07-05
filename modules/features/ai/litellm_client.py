import logging
from typing import Any, Dict, List

import litellm

from core import config


LITELLM_PREFIXES = ("openai/", "azure/", "gemini/", "zai/")
PROVIDER_ALIASES = {
    "openai": "openai",
    "azure": "azure",
    "gemini": "gemini",
    "zhipu": "zai",
    "zai": "zai",
}


def normalize_provider(provider: str) -> str:
    normalized = (provider or "").strip().lower()
    if normalized not in PROVIDER_ALIASES:
        raise RuntimeError(f"Unsupported AI provider: {provider}")
    return PROVIDER_ALIASES[normalized]


def _prefixed_model(provider: str, model: str) -> str:
    if not model:
        raise RuntimeError(f"Missing model configuration for provider: {provider}")
    if model.startswith(LITELLM_PREFIXES):
        return model
    return f"{provider}/{model}"


def _litellm_model(provider: str, model: str) -> str:
    if provider == "gemini" and config.GEMINI_API_BASE:
        return _prefixed_model("openai", model)
    return _prefixed_model(provider, model)


def _azure_api_base() -> str:
    if config.AZURE_OPENAI_API_ENDPOINT:
        return config.AZURE_OPENAI_API_ENDPOINT.rstrip("/")

    base_url = config.AZURE_OPENAI_BASE_URL or ""
    marker = "/openai/deployments/"
    if marker in base_url:
        return base_url.split(marker, 1)[0].rstrip("/")
    return base_url.rstrip("/")


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
        params = {"api_key": config.GEMINI_API_KEY}
        if config.GEMINI_API_BASE:
            params["api_base"] = config.GEMINI_API_BASE
        return params

    if provider == "zai":
        if not config.ZAI_API_KEY:
            raise RuntimeError("Missing ZAI_API_KEY configuration.")
        params = {"api_key": config.ZAI_API_KEY}
        if config.ZAI_API_BASE:
            params["api_base"] = config.ZAI_API_BASE
        return params

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
    request_kwargs = {
        key: value
        for key, value in kwargs.items()
        if value is not None
    }
    request_kwargs.setdefault("drop_params", True)

    litellm_model = _litellm_model(litellm_provider, model)
    logging.debug("Calling LiteLLM provider=%s model=%s", litellm_provider, litellm_model)

    return litellm.completion(
        model=litellm_model,
        messages=messages,
        **_provider_params(litellm_provider),
        **request_kwargs,
    )
