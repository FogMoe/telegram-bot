"""由 provider 声明表构造 LiteLLM 的认证与端点参数。"""

from typing import Any

from fogmoe_telegram_bot.core import ai_providers
from fogmoe_telegram_bot.core.ai_providers import BaseUrlStyle

# 没有 key 但配置了自定义 base URL 时使用的占位 key（OpenAI-compatible 的本地端点）。
KEYLESS_PLACEHOLDER_API_KEY = "sk-no-key-required"


def azure_api_base(settings: Any = None) -> str:
    read = ai_providers.read_setting
    credentials = ai_providers.require("azure").credentials
    endpoint = read(settings, credentials.api_base) if credentials.api_base else None
    if endpoint:
        return endpoint.rstrip("/")

    fallback = credentials.api_base_fallback
    base_url = (read(settings, fallback) if fallback else None) or ""
    marker = "/openai/deployments/"
    if marker in base_url:
        return base_url.split(marker, 1)[0].rstrip("/")
    return base_url.rstrip("/")


def openai_compatible_api_base(value: str | None) -> str:
    base_url = (value or "").rstrip("/")
    suffix = "/chat/completions"
    if base_url.lower().endswith(suffix):
        return base_url[: -len(suffix)].rstrip("/")
    return base_url


def gemini_native_api_base(value: str | None) -> str:
    base_url = (value or "").rstrip("/")
    suffix = "/models"
    if base_url.lower().endswith(suffix):
        return base_url[: -len(suffix)].rstrip("/")
    return base_url


def _missing(key: str) -> RuntimeError:
    return RuntimeError(f"Missing {key} configuration.")


def _raw_api_base(spec: ai_providers.ProviderSpec, settings: Any) -> Any:
    key = spec.credentials.api_base
    return ai_providers.read_setting(settings, key) if key else None


def _api_base(
    spec: ai_providers.ProviderSpec,
    settings: Any,
) -> str | None:
    """按声明的整理方式得到 api_base；没有配置返回 None 或空串（由调用方判断是否必需）。"""
    credentials = spec.credentials
    raw = _raw_api_base(spec, settings)

    style = credentials.base_style
    if style is BaseUrlStyle.AZURE:
        return azure_api_base(settings)
    if style is BaseUrlStyle.OPENAI_COMPATIBLE:
        return openai_compatible_api_base(raw)
    if style is BaseUrlStyle.GEMINI:
        if not raw:
            return None
        if spec.uses_openai_compatible_endpoint(settings):
            return openai_compatible_api_base(raw)
        return gemini_native_api_base(raw)
    return raw or None


def provider_params(provider: str, settings: Any = None) -> dict[str, Any]:
    spec = ai_providers.require(provider)
    credentials = spec.credentials
    read = ai_providers.read_setting

    api_key = read(settings, credentials.api_key)
    if not api_key and credentials.keyless_with_base and _raw_api_base(spec, settings):
        api_key = KEYLESS_PLACEHOLDER_API_KEY
    if not api_key:
        raise _missing(credentials.api_key)

    if (
        spec.openai_compatible_flag
        and spec.uses_openai_compatible_endpoint(settings)
        and not _raw_api_base(spec, settings)
    ):
        raise RuntimeError(f"{spec.openai_compatible_flag} requires {credentials.api_base}.")

    params: dict[str, Any] = {"api_key": api_key}
    api_base = _api_base(spec, settings)
    if credentials.base_required and not api_base:
        raise _missing(credentials.api_base or "api_base")
    if api_base:
        params["api_base"] = api_base

    if credentials.api_version:
        api_version = read(settings, credentials.api_version)
        if not api_version:
            raise _missing(credentials.api_version)
        params["api_version"] = api_version
    return params
