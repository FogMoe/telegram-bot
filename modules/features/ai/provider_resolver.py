from typing import Any, Dict, List

from core import config
from core.litellm_models import normalize_provider


TASKS = {"chat", "summary", "translate", "vision", "classifier", "advisor"}

TASK_PROVIDER_CONFIG_PREFIXES = {
    "summary": "AI_SUMMARY",
    "translate": "AI_TRANSLATE",
    "vision": "AI_VISION",
    "classifier": "AI_CLASSIFIER",
    "advisor": "AI_ADVISOR",
}

PROVIDER_MODEL_CONFIG_PATTERNS = {
    "openai": "OPENAI_{task}_MODEL",
    "siliconflow": "SILICONFLOW_{task}_MODEL",
    "gemini": "GEMINI_{task}_MODEL",
    "zai": "ZHIPU_{task}_MODEL",
    "azure": "AZURE_OPENAI_{task}_MODEL",
}

PROVIDER_FALLBACK_MODEL_CONFIGS = {
    ("gemini", "chat"): "GEMINI_CHAT_FALLBACK_MODEL",
    ("gemini", "summary"): "GEMINI_SUMMARY_FALLBACK_MODEL",
}

GEMINI_NATIVE_REASONING_EXCLUDED_TASKS = {"translate", "classifier"}


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

    env_prefix = TASK_PROVIDER_CONFIG_PREFIXES[task_name]
    primary = getattr(config, f"{env_prefix}_PROVIDER", None)
    fallback = getattr(config, f"{env_prefix}_FALLBACK_PROVIDER", None)
    return _dedupe([primary, fallback], lower=True)


def provider_model_for_task(provider: str, task: str) -> str | None:
    provider_name = normalize_provider(provider)
    task_name = task.lower()
    task_suffix = task_name.upper()
    config_pattern = PROVIDER_MODEL_CONFIG_PATTERNS.get(provider_name)
    if not config_pattern:
        return None
    return getattr(config, config_pattern.format(task=task_suffix), None)


def provider_fallback_model_for_task(provider: str, task: str) -> str | None:
    provider_name = normalize_provider(provider)
    task_name = task.lower()
    config_name = PROVIDER_FALLBACK_MODEL_CONFIGS.get((provider_name, task_name))
    if not config_name:
        return None
    return getattr(config, config_name, None)


def get_models_for_task(provider: str, task: str) -> List[str]:
    return _dedupe(
        [
            provider_model_for_task(provider, task),
            provider_fallback_model_for_task(provider, task),
        ]
    )


def completion_kwargs_for_task(provider: str, task: str) -> Dict[str, Any]:
    provider_name = normalize_provider(provider)
    task_name = task.lower()
    if (
        provider_name == "gemini"
        and not config.GEMINI_OPENAI_COMPATIBLE
        and task_name not in GEMINI_NATIVE_REASONING_EXCLUDED_TASKS
    ):
        return {"reasoning_effort": "high"}
    return {}
