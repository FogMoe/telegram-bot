import logging
from typing import Any, Dict, List

import litellm
from litellm.llms.custom_httpx.http_handler import HTTPHandler

from core import config
from core.litellm_models import litellm_model_name, normalize_provider
from .context_budget import enforce_messages_context_budget
from .litellm_message_sanitizer import (
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


class _GeminiNativeHTTPHandler(HTTPHandler):
    """Use Gemini's canonical JSON name for custom native endpoints."""

    def post(self, *args: Any, json: Any = None, **kwargs: Any) -> Any:
        if isinstance(json, dict) and "system_instruction" in json:
            json = dict(json)
            system_instruction = json.pop("system_instruction")
            json.setdefault("systemInstruction", system_instruction)
        return super().post(*args, json=json, **kwargs)


def _needs_gemini_native_http_compat(
    provider: str,
    messages: List[Dict[str, Any]],
) -> bool:
    return (
        provider == "gemini"
        and bool(config.GEMINI_API_BASE)
        and not config.GEMINI_OPENAI_COMPATIBLE
        and any(message.get("role") == "system" for message in messages)
    )


def create_chat_completion(
    provider: str,
    model: str,
    messages: List[Dict[str, Any]],
    *,
    context_hard_limit_ratio: float | None = None,
    **kwargs: Any,
) -> Any:
    litellm_provider = normalize_provider(provider)
    request_kwargs = {
        key: value
        for key, value in kwargs.items()
        if value is not None
    }
    max_output_tokens = request_kwargs.get(
        "max_completion_tokens",
        request_kwargs.get("max_tokens", 0),
    )
    budget_result = enforce_messages_context_budget(
        messages,
        token_limit=int(
            config.CHAT_TOKEN_LIMIT
            * (
                config.CHAT_CONTEXT_HARD_LIMIT_RATIO
                if context_hard_limit_ratio is None
                else context_hard_limit_ratio
            )
        ),
        max_output_tokens=int(max_output_tokens or 0),
        safety_tokens=config.CHAT_CONTEXT_SAFETY_TOKENS,
        model=model,
        tools=request_kwargs.get("tools"),
    )
    history_provider = (
        "openai"
        if litellm_provider == "gemini" and config.GEMINI_OPENAI_COMPATIBLE
        else litellm_provider
    )
    provider_messages = _sanitize_messages_for_provider(
        budget_result.messages,
        history_provider,
    )
    request_kwargs.setdefault("drop_params", True)

    litellm_model = litellm_model_name(litellm_provider, model)
    logging.debug("Calling LiteLLM provider=%s model=%s", litellm_provider, litellm_model)

    compat_client = None
    if (
        "client" not in request_kwargs
        and _needs_gemini_native_http_compat(litellm_provider, provider_messages)
    ):
        compat_client = _GeminiNativeHTTPHandler(
            timeout=request_kwargs.get("timeout"),
        )
        request_kwargs["client"] = compat_client

    try:
        return litellm.completion(
            model=litellm_model,
            messages=provider_messages,
            **_provider_params(litellm_provider),
            **request_kwargs,
        )
    finally:
        if compat_client is not None:
            compat_client.close()
