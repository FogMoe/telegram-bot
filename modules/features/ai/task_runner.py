import logging
from typing import Any, Dict, List

from core import config

from .litellm_client import create_chat_completion, normalize_provider


TASKS = {"chat", "summary", "translate", "vision", "classifier"}


def _dedupe(values: List[str | None], *, lower: bool = False) -> List[str]:
    seen = set()
    result: List[str] = []
    for value in values:
        if not value:
            continue
        normalized = value.strip()
        if not normalized or normalized in seen:
            continue
        key = normalized.lower() if lower else normalized
        if key in seen:
            continue
        seen.add(key)
        result.append(key if lower else normalized)
    return result


def get_provider_order_for_task(task: str) -> List[str]:
    task_name = task.lower()
    if task_name == "chat":
        return list(config.AI_SERVICE_ORDER)
    if task_name not in TASKS:
        raise RuntimeError(f"Unsupported AI task: {task}")

    env_prefix = f"AI_{task_name.upper()}"
    primary = getattr(config, f"{env_prefix}_PROVIDER", None)
    fallback = getattr(config, f"{env_prefix}_FALLBACK_PROVIDER", None)
    return _dedupe([primary, fallback], lower=True)


def _provider_model(provider: str, task: str) -> str | None:
    provider_name = normalize_provider(provider)
    task_name = task.lower()
    task_suffix = task_name.upper()

    if provider_name == "openai":
        return getattr(config, f"OPENAI_{task_suffix}_MODEL", None)

    if provider_name == "gemini":
        return getattr(config, f"GEMINI_{task_suffix}_MODEL", None)

    if provider_name == "zai":
        zhipu_task_models = {
            "chat": config.ZHIPU_CHAT_MODEL,
            "summary": config.ZHIPU_SUMMARY_MODEL,
            "translate": config.ZHIPU_TRANSLATE_MODEL,
            "vision": config.ZHIPU_VISION_MODEL,
            "classifier": config.ZHIPU_CLASSIFIER_MODEL,
        }
        return zhipu_task_models.get(task_name)

    if provider_name == "azure":
        return getattr(config, f"AZURE_OPENAI_{task_suffix}_MODEL", None)

    return None


def _provider_fallback_model(provider: str, task: str) -> str | None:
    provider_name = normalize_provider(provider)
    task_name = task.lower()
    if provider_name == "gemini" and task_name == "summary":
        return config.GEMINI_SUMMARY_FALLBACK_MODEL
    if provider_name == "gemini" and task_name == "chat":
        return config.GEMINI_CHAT_FALLBACK_MODEL
    return None


def get_models_for_task(provider: str, task: str) -> List[str]:
    return _dedupe([
        _provider_model(provider, task),
        _provider_fallback_model(provider, task),
    ])


def run_ai_task(
    task: str,
    messages: List[Dict[str, Any]],
    **kwargs: Any,
) -> Any:
    last_error: Exception | None = None
    for provider in get_provider_order_for_task(task):
        models = get_models_for_task(provider, task)
        if not models:
            logging.warning("AI task %s skipped provider %s: no model configured", task, provider)
            continue

        for model in models:
            try:
                return create_chat_completion(provider, model, messages, **kwargs)
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
