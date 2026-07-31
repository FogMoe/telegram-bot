import logging
from typing import Dict, Optional

from core import config

from ..provider_resolver import completion_kwargs_for_task
from ..tool_runner import run_tool_loop
from ..types import AIResponse, VisibleContentHandler


def get_ai_response(
    messages,
    user_id: int,
    tool_context: Optional[Dict[str, object]] = None,
    visible_content_handler: Optional[VisibleContentHandler] = None,
) -> AIResponse:
    """通过 FOGMOE OpenAI-compatible endpoint 生成响应。"""
    model = config.FOGMOE_CHAT_MODEL
    if not model:
        raise RuntimeError("Missing FOGMOE_CHAT_MODEL configuration.")

    try:
        return run_tool_loop(
            "fogmoe",
            model,
            messages,
            tool_context,
            provider_name="FOGMOE",
            completion_kwargs=completion_kwargs_for_task("fogmoe", "chat"),
            visible_content_handler=visible_content_handler,
        )
    except Exception as exc:
        logging.error("FOGMOE AI 请求失败: %s", exc)
        raise
