import logging
from typing import Any, Dict, List

from .litellm_client import create_chat_completion
from .provider_resolver import (
    TASKS,
    _dedupe,
    completion_kwargs_for_task,
    get_models_for_task,
    get_provider_order_for_task,
    provider_fallback_model_for_task,
    provider_model_for_task,
)


def _provider_model(provider: str, task: str) -> str | None:
    return provider_model_for_task(provider, task)


def _provider_fallback_model(provider: str, task: str) -> str | None:
    return provider_fallback_model_for_task(provider, task)


def _provider_completion_kwargs(provider: str, task: str) -> Dict[str, Any]:
    return completion_kwargs_for_task(provider, task)


def run_ai_task(
    task: str,
    messages: List[Dict[str, Any]],
    **kwargs: Any,
) -> Any:
    last_error: Exception | None = None
    for provider in get_provider_order_for_task(task):
        try:
            models = get_models_for_task(provider, task)
        except Exception as exc:
            logging.warning(
                "AI task %s skipped invalid provider=%s: %s",
                task,
                provider,
                exc,
            )
            last_error = exc
            continue
        if not models:
            logging.warning("AI task %s skipped provider %s: no model configured", task, provider)
            continue

        for model in models:
            try:
                request_kwargs = {
                    **_provider_completion_kwargs(provider, task),
                    **kwargs,
                }
                return create_chat_completion(provider, model, messages, **request_kwargs)
            except Exception as exc:
                logging.warning(
                    "AI task %s failed via provider=%s model=%s: %s",
                    task,
                    provider,
                    model,
                    exc,
                )
                last_error = exc

    raise RuntimeError(f"All providers failed for AI task: {task}") from last_error
