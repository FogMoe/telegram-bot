from __future__ import annotations

from . import config


LITELLM_PREFIXES = ("openai/", "openrouter/", "azure/", "gemini/", "zai/")
PROVIDER_ALIASES = {
    "openai": "openai",
    "openrouter": "openrouter",
    "fogmoe": "fogmoe",
    "azure": "azure",
    "gemini": "gemini",
    "siliconflow": "siliconflow",
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


def litellm_model_name(provider: str, model: str) -> str:
    provider_name = normalize_provider(provider)
    if provider_name == "openrouter":
        if not model:
            raise RuntimeError("Missing model configuration for provider: openrouter")
        if model.startswith("openrouter/"):
            return model
        return f"openrouter/{model}"
    if provider_name == "fogmoe":
        if not model:
            raise RuntimeError("Missing model configuration for provider: fogmoe")
        return f"openai/{model}"
    if provider_name == "gemini" and config.GEMINI_OPENAI_COMPATIBLE:
        return _prefixed_model("openai", model)
    if provider_name == "siliconflow":
        return _prefixed_model("openai", model)
    return _prefixed_model(provider_name, model)
