import asyncio
import logging
from typing import Dict, Optional

from core import config

from .tools import clear_tool_request_context, set_tool_request_context
from .errors import SafetyBlockError
from .providers import azure, gemini, zhipu
from .runtime import EXECUTOR
from .types import AIResponse

AI_SERVICE_MAP = {
    "gemini": gemini.get_ai_response,
    "azure": azure.get_ai_response,
    "zhipu": zhipu.get_ai_response,
}

AI_SERVICE_ORDER = config.AI_SERVICE_ORDER


def _call_service_with_context(
    service_name: str,
    messages,
    user_id: int,
    tool_context: Optional[Dict[str, object]],
) -> AIResponse:
    set_tool_request_context(dict(tool_context or {}))
    try:
        return AI_SERVICE_MAP[service_name](messages, user_id, tool_context)
    finally:
        clear_tool_request_context()


async def get_ai_response(
    messages,
    user_id: int,
    tool_context: Optional[Dict[str, object]] = None,
) -> AIResponse:
    """
    统一AI响应异步接口，根据配置的顺序依次尝试不同的AI服务
    """
    last_error = None
    loop = asyncio.get_running_loop()

    for service_name in AI_SERVICE_ORDER:
        try:
            response = await loop.run_in_executor(
                EXECUTOR,
                lambda s=service_name: _call_service_with_context(
                    s, messages.copy(), user_id, tool_context
                ),
            )
            return response
        except SafetyBlockError:
            if service_name == "gemini":
                logging.warning("Gemini triggered safety block, trying next service")
                last_error = SafetyBlockError("SafetyBlockError")
                continue
            raise
        except Exception as exc:
            logging.warning("%s 调用失败: %s", service_name, exc)
            last_error = exc
            continue

    logging.error("所有AI服务均调用失败: %s", last_error)
    return (
        "抱歉喵，雾萌娘在处理你的请求时遇到了一点小问题！现在有点不舒服啦，请稍后再试吧～\n"
        "请联系管理员 @ScarletKc 反馈问题。",
        [],
    )
