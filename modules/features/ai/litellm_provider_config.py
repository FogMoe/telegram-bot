from typing import Any, Dict

from core import config


def azure_api_base() -> str:
    if config.AZURE_OPENAI_API_ENDPOINT:
        return config.AZURE_OPENAI_API_ENDPOINT.rstrip("/")

    base_url = config.AZURE_OPENAI_BASE_URL or ""
    marker = "/openai/deployments/"
    if marker in base_url:
        return base_url.split(marker, 1)[0].rstrip("/")
    return base_url.rstrip("/")


def openai_compatible_api_base(value: str) -> str:
    base_url = (value or "").rstrip("/")
    suffix = "/chat/completions"
    if base_url.lower().endswith(suffix):
        return base_url[: -len(suffix)].rstrip("/")
    return base_url


def gemini_native_api_base(value: str) -> str:
    base_url = (value or "").rstrip("/")
    suffix = "/models"
    if base_url.lower().endswith(suffix):
        return base_url[: -len(suffix)].rstrip("/")
    return base_url


def _openai_params() -> Dict[str, Any]:
    api_key = config.OPENAI_API_KEY
    if not api_key and config.OPENAI_BASE_URL:
        api_key = "sk-no-key-required"
    if not api_key:
        raise RuntimeError("Missing OPENAI_API_KEY configuration.")

    params: Dict[str, Any] = {"api_key": api_key}
    if config.OPENAI_BASE_URL:
        params["api_base"] = config.OPENAI_BASE_URL
    return params


def _gemini_params() -> Dict[str, Any]:
    if not config.GEMINI_API_KEY:
        raise RuntimeError("Missing GEMINI_API_KEY configuration.")
    if config.GEMINI_OPENAI_COMPATIBLE and not config.GEMINI_API_BASE:
        raise RuntimeError("GEMINI_OPENAI_COMPATIBLE requires GEMINI_API_BASE.")
    params = {"api_key": config.GEMINI_API_KEY}
    if config.GEMINI_API_BASE:
        params["api_base"] = (
            openai_compatible_api_base(config.GEMINI_API_BASE)
            if config.GEMINI_OPENAI_COMPATIBLE
            else gemini_native_api_base(config.GEMINI_API_BASE)
        )
    return params


def _zai_params() -> Dict[str, Any]:
    if not config.ZAI_API_KEY:
        raise RuntimeError("Missing ZAI_API_KEY configuration.")
    params = {"api_key": config.ZAI_API_KEY}
    if config.ZAI_API_BASE:
        params["api_base"] = config.ZAI_API_BASE
    return params


def _siliconflow_params() -> Dict[str, Any]:
    if not config.SILICONFLOW_API_KEY:
        raise RuntimeError("Missing SILICONFLOW_API_KEY configuration.")
    api_base = openai_compatible_api_base(config.SILICONFLOW_API_BASE)
    if not api_base:
        raise RuntimeError("Missing SILICONFLOW_API_BASE configuration.")
    return {
        "api_key": config.SILICONFLOW_API_KEY,
        "api_base": api_base,
    }


def _azure_params() -> Dict[str, Any]:
    if not config.AZURE_OPENAI_API_KEY:
        raise RuntimeError("Missing AZURE_OPENAI_API_KEY configuration.")
    api_base = azure_api_base()
    if not api_base:
        raise RuntimeError("Missing AZURE_OPENAI_API_ENDPOINT configuration.")
    if not config.AZURE_OPENAI_API_VERSION:
        raise RuntimeError("Missing AZURE_OPENAI_API_VERSION configuration.")
    return {
        "api_key": config.AZURE_OPENAI_API_KEY,
        "api_base": api_base,
        "api_version": config.AZURE_OPENAI_API_VERSION,
    }


PROVIDER_PARAM_BUILDERS = {
    "openai": _openai_params,
    "gemini": _gemini_params,
    "zai": _zai_params,
    "siliconflow": _siliconflow_params,
    "azure": _azure_params,
}


def provider_params(provider: str) -> Dict[str, Any]:
    builder = PROVIDER_PARAM_BUILDERS.get(provider)
    if not builder:
        raise RuntimeError(f"Unsupported AI provider: {provider}")
    return builder()
