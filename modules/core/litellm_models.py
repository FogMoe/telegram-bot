"""provider 名字与 LiteLLM 模型名的转换。规则由 `core.ai_providers` 的声明表决定。"""

from __future__ import annotations

from typing import Any

from . import ai_providers
from .ai_providers import LITELLM_PREFIXES

__all__ = ["LITELLM_PREFIXES", "litellm_model_name", "normalize_provider"]


def normalize_provider(provider: str) -> str:
    """返回 provider 的规范名（`zhipu` 归一为 `zai`）；未知名字抛 RuntimeError。"""
    return ai_providers.require(provider).name


def litellm_model_name(provider: str, model: str | None, settings: Any = None) -> str:
    return ai_providers.litellm_model_name(provider, model, settings)
