import logging
from typing import Dict, Optional

from core import config

from ..clients import create_gemini_client
from ..errors import SafetyBlockError
from ..tool_runner import run_tool_loop
from ..types import AIResponse


def get_ai_response(
    messages,
    user_id: int,
    tool_context: Optional[Dict[str, object]] = None,
) -> AIResponse:
    """同步版本的Google Gemini响应函数（OpenAI兼容接口）。"""
    client = create_gemini_client()

    primary_model = config.GEMINI_MODEL
    fallback_model = config.GEMINI_FALLBACK_MODEL

    def _run(model_name: str) -> AIResponse:
        return run_tool_loop(
            client,
            model_name,
            messages,
            tool_context,
            provider_name="Gemini",
        )

    try:
        return _run(primary_model)
    except Exception as exc:
        error_str = str(exc)
        if fallback_model and fallback_model != primary_model:
            logging.warning(
                "Gemini 主模型失败，尝试回退模型 %s: %s",
                fallback_model,
                error_str,
            )
            return _run(fallback_model)
        if "SAFETY" in error_str and "blocked" in error_str:
            logging.warning("Gemini safety block triggered: %s", error_str)
            raise SafetyBlockError(error_str) from exc

        logging.error("Google Gemini 请求失败: %s", error_str)
        raise

