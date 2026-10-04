import logging
import time
from typing import Any, Dict, List

import litellm
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

from core import ai_providers, blocking, config, metrics
from core.litellm_models import litellm_model_name, normalize_provider
from .context_budget import enforce_messages_context_budget
from .errors import is_timeout_error
from .litellm_message_sanitizer import sanitize_messages_for_provider
from .litellm_provider_config import provider_params


def _provider_params(provider: str) -> Dict[str, Any]:
    return provider_params(provider)


class _GeminiNativeAsyncHTTPHandler(AsyncHTTPHandler):
    """Use Gemini's canonical JSON name for custom native endpoints."""

    async def post(self, *args: Any, json: Any = None, **kwargs: Any) -> Any:
        if isinstance(json, dict) and "system_instruction" in json:
            json = dict(json)
            system_instruction = json.pop("system_instruction")
            json.setdefault("systemInstruction", system_instruction)
        return await super().post(*args, json=json, **kwargs)


def _needs_native_http_compat(
    spec: ai_providers.ProviderSpec,
    wire: ai_providers.WireProtocol,
    messages: List[Dict[str, Any]],
) -> bool:
    """自定义的原生端点只认 `systemInstruction`，请求里带 system 消息时需要改名的 HTTP 客户端。"""
    api_base_key = spec.credentials.api_base
    return (
        wire.camel_case_system_instruction
        and bool(api_base_key and ai_providers.read_setting(None, api_base_key))
        and any(message.get("role") == "system" for message in messages)
    )


async def create_chat_completion(
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
    token_limit = int(
        config.CHAT_TOKEN_LIMIT
        * (
            config.CHAT_CONTEXT_HARD_LIMIT_RATIO
            if context_hard_limit_ratio is None
            else context_hard_limit_ratio
        )
    )
    spec = ai_providers.require(litellm_provider)
    wire = spec.wire_protocol_for()

    def prepare_messages() -> List[Dict[str, Any]]:
        # token 计数是 CPU 密集的同步计算（长历史可达上百毫秒），放在适配器线程里，不占事件循环。
        budget_result = enforce_messages_context_budget(
            messages,
            token_limit=token_limit,
            max_output_tokens=int(max_output_tokens or 0),
            safety_tokens=config.CHAT_CONTEXT_SAFETY_TOKENS,
            model=model,
            tools=request_kwargs.get("tools"),
        )
        return sanitize_messages_for_provider(
            budget_result.messages,
            litellm_provider,
            protocol=wire,
        )

    provider_messages = await blocking.compute().run(prepare_messages)
    request_kwargs.setdefault("drop_params", True)

    litellm_model = litellm_model_name(litellm_provider, model)
    logging.debug("Calling LiteLLM provider=%s model=%s", litellm_provider, litellm_model)

    compat_client = None
    if "client" not in request_kwargs and _needs_native_http_compat(
        spec, wire, provider_messages
    ):
        compat_client = _GeminiNativeAsyncHTTPHandler(
            timeout=request_kwargs.get("timeout"),
        )
        request_kwargs["client"] = compat_client

    started = time.perf_counter()
    try:
        response = await litellm.acompletion(
            model=litellm_model,
            messages=provider_messages,
            **_provider_params(litellm_provider),
            **request_kwargs,
        )
    except Exception as exc:
        metrics.counter("provider.failures", provider=litellm_provider).inc()
        if is_timeout_error(exc):
            metrics.counter("provider.timeouts", provider=litellm_provider).inc()
        raise
    else:
        metrics.histogram("provider.call_seconds", provider=litellm_provider).observe(
            time.perf_counter() - started
        )
        return response
    finally:
        metrics.counter("provider.calls", provider=litellm_provider).inc()
        if compat_client is not None:
            await compat_client.close()


async def close_clients() -> None:
    """关闭 LiteLLM 缓存的异步 HTTP 客户端（进程停止时调用）。失败只记录，不影响停止流程。"""
    try:
        await litellm.close_litellm_async_clients()
    except Exception:
        logging.warning("Failed to close LiteLLM async clients", exc_info=True)
