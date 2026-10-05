import logging
from typing import Any

from .context_budget import ContextBudgetExceededError
from .litellm_client import create_chat_completion
from .provider_resolver import (
    completion_kwargs_for_task,
    get_models_for_task,
    get_provider_order_for_task,
    missing_capability_for_task,
    provider_fallback_model_for_task,
    provider_model_for_task,
)


def _provider_model(provider: str, task: str) -> str | None:
    return provider_model_for_task(provider, task)


def _provider_fallback_model(provider: str, task: str) -> str | None:
    return provider_fallback_model_for_task(provider, task)


def _provider_completion_kwargs(provider: str, task: str) -> dict[str, Any]:
    return completion_kwargs_for_task(provider, task)


async def run_ai_task(
    task: str,
    messages: list[dict[str, Any]],
    **kwargs: Any,
) -> Any:
    last_error: Exception | None = None
    for provider in get_provider_order_for_task(task):
        missing_capability = missing_capability_for_task(provider, task)
        if missing_capability:
            logging.warning(
                "AI task %s skipped provider %s: no %s support",
                task,
                provider,
                missing_capability,
            )
            continue
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
                return await create_chat_completion(provider, model, messages, **request_kwargs)
            except ContextBudgetExceededError:
                raise
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
