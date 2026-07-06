import os
import time
from dataclasses import dataclass
from typing import Iterable

import pytest

from core import config
from features.ai.litellm_client import create_chat_completion


TRUTHY_VALUES = {"1", "true", "yes", "on"}


def _enabled(value: str | None) -> bool:
    return str(value or "").strip().lower() in TRUTHY_VALUES


@dataclass(frozen=True)
class ProviderConfig:
    model: str | None
    model_env: str
    base_url: str
    key_present: bool


PROVIDER_CONFIGS = {
    "openai": ProviderConfig(
        model=config.OPENAI_CHAT_MODEL,
        model_env="OPENAI_CHAT_MODEL",
        base_url=config.OPENAI_BASE_URL or "<default>",
        key_present=bool(config.OPENAI_API_KEY or config.OPENAI_BASE_URL),
    ),
    "azure": ProviderConfig(
        model=config.AZURE_OPENAI_CHAT_MODEL,
        model_env="AZURE_OPENAI_CHAT_MODEL",
        base_url=(
            config.AZURE_OPENAI_API_ENDPOINT
            or config.AZURE_OPENAI_BASE_URL
            or "<missing>"
        ),
        key_present=bool(config.AZURE_OPENAI_API_KEY),
    ),
    "gemini": ProviderConfig(
        model=config.GEMINI_CHAT_MODEL,
        model_env="GEMINI_CHAT_MODEL",
        base_url=config.GEMINI_API_BASE or "<default>",
        key_present=bool(config.GEMINI_API_KEY),
    ),
    "siliconflow": ProviderConfig(
        model=config.SILICONFLOW_CHAT_MODEL,
        model_env="SILICONFLOW_CHAT_MODEL",
        base_url=config.SILICONFLOW_API_BASE or "<missing>",
        key_present=bool(config.SILICONFLOW_API_KEY),
    ),
    "zhipu": ProviderConfig(
        model=config.ZHIPU_CHAT_MODEL,
        model_env="ZHIPU_CHAT_MODEL",
        base_url=config.ZAI_API_BASE or "<default>",
        key_present=bool(config.ZAI_API_KEY),
    ),
}


def _selected_providers() -> list[str]:
    raw_value = os.getenv("ENV_API_CONNECTIVITY_PROVIDERS")
    if raw_value is None:
        raw_value = ",".join(config.AI_SERVICE_ORDER)
    return [item.strip().lower() for item in raw_value.split(",") if item.strip()]


def _provider_model_cases(provider: str) -> Iterable[tuple[str, str]]:
    provider_config = PROVIDER_CONFIGS[provider]
    if provider_config.model:
        yield provider_config.model, provider_config.model_env

    if provider == "gemini" and config.GEMINI_CHAT_FALLBACK_MODEL:
        yield config.GEMINI_CHAT_FALLBACK_MODEL, "GEMINI_CHAT_FALLBACK_MODEL"


def _redact_secrets(value: object) -> str:
    text = str(value)
    for secret in (
        config.OPENAI_API_KEY,
        config.AZURE_OPENAI_API_KEY,
        config.GEMINI_API_KEY,
        config.SILICONFLOW_API_KEY,
        config.ZAI_API_KEY,
    ):
        if secret:
            text = text.replace(secret, "***")
    return text


def _response_content(response: object) -> str:
    try:
        message = response.choices[0].message
        if isinstance(message, dict):
            return str(message.get("content") or "")
        return str(getattr(message, "content", "") or "")
    except Exception:
        return ""


def test_configured_chat_provider_apis_are_reachable():
    if not _enabled(os.getenv("RUN_ENV_API_CONNECTIVITY_TESTS")):
        pytest.skip(
            "Set RUN_ENV_API_CONNECTIVITY_TESTS=1 to call real APIs from the "
            "current .env."
        )

    providers = _selected_providers()
    if not providers:
        pytest.fail(
            "No providers selected. Set AI_CHAT_ORDER or ENV_API_CONNECTIVITY_PROVIDERS."
        )

    unknown_providers = [item for item in providers if item not in PROVIDER_CONFIGS]
    if unknown_providers:
        pytest.fail(
            "Unknown provider(s): "
            f"{', '.join(unknown_providers)}. "
            f"Supported: {', '.join(sorted(PROVIDER_CONFIGS))}."
        )

    timeout_seconds = int(os.getenv("ENV_API_CONNECTIVITY_TIMEOUT", "25"))
    failures: list[str] = []

    for provider in providers:
        provider_config = PROVIDER_CONFIGS[provider]
        if not provider_config.key_present:
            failures.append(f"{provider}: missing API key configuration")
            continue

        model_cases = list(_provider_model_cases(provider))
        if not model_cases:
            failures.append(f"{provider}: missing {provider_config.model_env}")
            continue

        for model, model_env in model_cases:
            started = time.perf_counter()
            try:
                response = create_chat_completion(
                    provider,
                    model,
                    [{"role": "user", "content": "Reply with exactly: ok"}],
                    max_tokens=8,
                    temperature=0,
                    timeout=timeout_seconds,
                )
                elapsed = time.perf_counter() - started
                if not getattr(response, "choices", None):
                    raise AssertionError("response has no choices")
                print(
                    "[env-api] "
                    f"provider={provider} model_env={model_env} model={model} "
                    f"base={provider_config.base_url} status=OK "
                    f"elapsed={elapsed:.2f}s content={_response_content(response)!r}"
                )
            except Exception as exc:
                elapsed = time.perf_counter() - started
                failures.append(
                    "[env-api] "
                    f"provider={provider} model_env={model_env} model={model} "
                    f"base={provider_config.base_url} status=FAIL "
                    f"elapsed={elapsed:.2f}s "
                    f"error={type(exc).__name__}: {_redact_secrets(exc)}"
                )

    if failures:
        pytest.fail("\n".join(failures))
