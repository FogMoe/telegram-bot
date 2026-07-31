import logging
from typing import Dict, Optional

from core import config

from ..tool_runner import run_tool_loop
from ..types import AIResponse, VisibleContentHandler


def get_ai_response(
    messages,
    user_id: int,
    tool_context: Optional[Dict[str, object]] = None,
    visible_content_handler: Optional[VisibleContentHandler] = None,
) -> AIResponse:
    """通过 LiteLLM 原生 OpenRouter provider 生成响应。"""
    model = config.OPENROUTER_CHAT_MODEL
    if not model:
        raise RuntimeError("Missing OPENROUTER_CHAT_MODEL configuration.")

    try:
        return run_tool_loop(
            "openrouter",
            model,
            messages,
            tool_context,
            provider_name="OpenRouter",
            visible_content_handler=visible_content_handler,
        )
    except Exception as exc:
        logging.error("OpenRouter 请求失败: %s", exc)
        raise
