"""主聊天的 provider 入口。

所有 provider 共用这一个入口：它按 `core.ai_providers` 里的声明取模型、展示名、需要跳过的工具，
再运行同一个工具循环。provider 之间的差异只来自声明：

- 备用模型（Gemini）：主模型失败后尝试 `*_CHAT_FALLBACK_MODEL`；
- 安全拦截（Gemini）：没有可用的备用模型时，把「被安全策略拦截」的错误转成 `SafetyBlockError`，
  由 router 决定是否换下一个 provider；
- 跳过的工具（Z.ai 不暴露 `web_search` 与 `web_browser`）。

上下文超限与工具执行后的部分失败原样上抛，由 router 统一处理与记录。
"""

import logging
from typing import Any, Dict, Optional

from core import ai_providers

from .context_budget import ContextBudgetExceededError
from .errors import SafetyBlockError
from .tool_runner import run_tool_loop
from .types import AIResponse, PartialAIResponseError, VisibleContentHandler


def run_chat_provider(
    service_name: str,
    messages: Any,
    user_id: int,
    tool_context: Optional[Dict[str, object]] = None,
    visible_content_handler: Optional[VisibleContentHandler] = None,
    settings: Any = None,
) -> AIResponse:
    """用 `service_name` 声明的 provider 运行一次主聊天的工具循环（同步，由 router 放进线程池）。"""
    spec = ai_providers.require(service_name)
    if not spec.supports("tools"):
        raise RuntimeError(f"{spec.display_name} does not support tool calling.")

    primary_model = ai_providers.configured_model(spec, "chat", settings)
    fallback_model = ai_providers.configured_fallback_model(spec, "chat", settings)

    def _run(model_name: str) -> AIResponse:
        return run_tool_loop(
            spec.name,
            model_name,
            messages,
            tool_context,
            provider_name=spec.display_name,
            skip_tools=spec.skip_tools,
            visible_content_handler=visible_content_handler,
        )

    try:
        if not primary_model:
            raise RuntimeError(f"Missing {spec.model_key('chat')} configuration.")
        return _run(primary_model)
    except (ContextBudgetExceededError, PartialAIResponseError):
        raise
    except Exception as exc:
        error_text = str(exc)
        if fallback_model and fallback_model != primary_model:
            logging.warning(
                "%s 主模型失败，尝试回退模型 %s: %s",
                spec.display_name,
                fallback_model,
                error_text,
            )
            return _run(fallback_model)
        if spec.translates_safety_blocks and "SAFETY" in error_text and "blocked" in error_text:
            logging.warning("%s safety block triggered: %s", spec.display_name, error_text)
            raise SafetyBlockError(error_text) from exc

        logging.error("%s 请求失败: %s", spec.display_name, error_text)
        raise
