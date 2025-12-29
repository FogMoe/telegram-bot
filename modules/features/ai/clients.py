from typing import Dict, Optional, Any

from openai import OpenAI

from core import config


def _build_openai_client(
    api_key: str,
    *,
    base_url: Optional[str] = None,
    default_headers: Optional[Dict[str, str]] = None,
    default_query: Optional[Dict[str, str]] = None,
) -> OpenAI:
    if not api_key:
        raise RuntimeError("Missing API key configuration.")
    client_kwargs: Dict[str, Any] = {"api_key": api_key}
    if base_url:
        client_kwargs["base_url"] = base_url
    if default_headers:
        client_kwargs["default_headers"] = default_headers
    if default_query:
        client_kwargs["default_query"] = default_query
    return OpenAI(**client_kwargs)


def create_zhipu_client() -> OpenAI:
    if not config.ZAI_API_KEY:
        raise RuntimeError("Missing ZAI_API_KEY configuration.")
    return _build_openai_client(
        config.ZAI_API_KEY,
        base_url=config.ZHIPU_BASE_URL,
    )


def create_gemini_client() -> OpenAI:
    if not config.GEMINI_API_KEY:
        raise RuntimeError("Missing GEMINI_API_KEY configuration.")
    return _build_openai_client(
        config.GEMINI_API_KEY,
        base_url=config.GEMINI_BASE_URL,
    )


def create_azure_client() -> OpenAI:
    if not config.AZURE_OPENAI_API_KEY:
        raise RuntimeError("Missing AZURE_OPENAI_API_KEY configuration.")
    if not config.AZURE_OPENAI_BASE_URL:
        raise RuntimeError("Missing AZURE_OPENAI_BASE_URL configuration.")
    default_query = {"api-version": config.AZURE_OPENAI_API_VERSION}
    default_headers = {"api-key": config.AZURE_OPENAI_API_KEY}
    return _build_openai_client(
        config.AZURE_OPENAI_API_KEY,
        base_url=config.AZURE_OPENAI_BASE_URL,
        default_headers=default_headers,
        default_query=default_query,
    )

