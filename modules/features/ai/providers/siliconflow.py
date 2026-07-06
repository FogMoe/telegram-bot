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
    """同步版本的 SiliconFlow 响应函数（OpenAI-compatible API）。"""
    siliconflow_model = config.SILICONFLOW_CHAT_MODEL
    if not siliconflow_model:
        raise RuntimeError("Missing SILICONFLOW_CHAT_MODEL configuration.")

    try:
        return run_tool_loop(
            "siliconflow",
            siliconflow_model,
            messages,
            tool_context,
            provider_name="SiliconFlow",
            visible_content_handler=visible_content_handler,
        )
    except Exception as exc:
        logging.error("SiliconFlow 请求失败: %s", exc)
        raise
