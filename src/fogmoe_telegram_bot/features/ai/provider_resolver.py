"""任务到 provider 与模型的解析。声明在 `core.ai_providers`，这里只按配置读取并整理顺序。"""

from typing import Any, Dict, List

from fogmoe_telegram_bot.core import ai_providers

TASKS = set(ai_providers.TASK_SPECS)


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


def get_provider_order_for_task(task: str, settings: Any = None) -> List[str]:
    task_name = task.lower()
    task_spec = ai_providers.TASK_SPECS.get(task_name)
    if task_spec is None:
        raise RuntimeError(f"Unsupported AI task: {task}")
    if task_spec.provider_config_prefix is None:
        return list(ai_providers.read_setting(settings, "AI_SERVICE_ORDER") or [])

    prefix = task_spec.provider_config_prefix
    primary = ai_providers.read_setting(settings, f"{prefix}_PROVIDER")
    fallback = ai_providers.read_setting(settings, f"{prefix}_FALLBACK_PROVIDER")
    return _dedupe([primary, fallback], lower=True)


def provider_model_for_task(provider: str, task: str, settings: Any = None) -> str | None:
    spec = ai_providers.require(provider)
    return ai_providers.configured_model(spec, task, settings)


def provider_fallback_model_for_task(
    provider: str,
    task: str,
    settings: Any = None,
) -> str | None:
    spec = ai_providers.require(provider)
    return ai_providers.configured_fallback_model(spec, task, settings)


def get_models_for_task(provider: str, task: str, settings: Any = None) -> List[str]:
    return _dedupe(
        [
            provider_model_for_task(provider, task, settings),
            provider_fallback_model_for_task(provider, task, settings),
        ]
    )


def missing_capability_for_task(provider: str, task: str) -> str | None:
    """已声明的 provider 缺少该任务要求的哪项能力（chat 要工具，vision 要视觉）；满足或未知返回 None。

    未知 provider 不在这里处理：它们在取模型时按「不支持的 provider」报错。
    """
    spec = ai_providers.lookup(provider)
    task_spec = ai_providers.TASK_SPECS.get(task.lower())
    if spec is None or task_spec is None:
        return None
    for capability in task_spec.requires:
        if not spec.supports(capability):
            return capability
    return None


def completion_kwargs_for_task(provider: str, task: str) -> Dict[str, Any]:
    return {}
