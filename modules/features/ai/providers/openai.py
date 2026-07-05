import logging
from typing import Dict, Optional

from core import config

from ..tool_runner import run_tool_loop
from ..types import AIResponse


def get_ai_response(
    messages,
    user_id: int,
    tool_context: Optional[Dict[str, object]] = None,
) -> AIResponse:
    """同步版本的 OpenAI 响应函数（支持工具调用）"""
    openai_model = config.OPENAI_CHAT_MODEL
    if not openai_model:
        raise RuntimeError("Missing OPENAI_CHAT_MODEL configuration.")

    try:
        return run_tool_loop(
            "openai",
            openai_model,
            messages,
            tool_context,
            provider_name="OpenAI",
        )
    except Exception as exc:
        logging.error("OpenAI 请求失败: %s", exc)
        raise
