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
    """同步版本的Azure OpenAI响应函数（支持工具调用）"""
    azure_model = config.AZURE_OPENAI_CHAT_MODEL
    if not azure_model:
        raise RuntimeError("Missing AZURE_OPENAI_CHAT_MODEL configuration.")

    try:
        return run_tool_loop(
            "azure",
            azure_model,
            messages,
            tool_context,
            provider_name="Azure",
            visible_content_handler=visible_content_handler,
        )
    except Exception as exc:
        logging.error("Azure OpenAI 请求失败: %s", exc)
        raise
