import asyncio
import logging
from typing import Any, Dict, Optional

from core import config

from .tools import clear_tool_request_context, set_tool_request_context
from .errors import SafetyBlockError
from .providers import azure, gemini, openai, zhipu
from .runtime import EXECUTOR
from .types import AIResponse

AI_SERVICE_MAP = {
    "openai": openai.get_ai_response,
    "gemini": gemini.get_ai_response,
    "azure": azure.get_ai_response,
    "zhipu": zhipu.get_ai_response,
    "zai": zhipu.get_ai_response,
}

AI_SERVICE_ORDER = config.AI_SERVICE_ORDER


def _content_has_image(content: Any) -> bool:
    if not isinstance(content, list):
        return False
    return any(isinstance(item, dict) and item.get("type") == "image_url" for item in content)


def _messages_have_images(messages) -> bool:
    return any(
        isinstance(message, dict) and _content_has_image(message.get("content"))
        for message in messages
    )


def _content_to_text(content: Any) -> str:
    if not isinstance(content, list):
        return content if isinstance(content, str) else str(content or "")

    text_parts = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            text = item.get("text")
            if text:
                text_parts.append(str(text))
    return "\n".join(text_parts)


def _strip_image_content(messages) -> list:
    stripped_messages = []
    for message in messages:
        if not isinstance(message, dict):
            stripped_messages.append(message)
            continue
        content = message.get("content")
        if not _content_has_image(content):
            stripped_messages.append(message)
            continue
        stripped_message = dict(message)
        stripped_message["content"] = _content_to_text(content)
        stripped_messages.append(stripped_message)
    return stripped_messages


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


async def _try_ai_services(
    messages,
    user_id: int,
    tool_context: Optional[Dict[str, object]] = None,
) -> tuple[AIResponse | None, Exception | None]:
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

    return None, last_error


async def get_ai_response(
    messages,
    user_id: int,
    tool_context: Optional[Dict[str, object]] = None,
    text_fallback_messages=None,
) -> AIResponse:
    """
    统一AI响应异步接口，根据配置的顺序依次尝试不同的AI服务
    """
    response, last_error = await _try_ai_services(messages, user_id, tool_context)
    if response is not None:
        return response

    if _messages_have_images(messages):
        logging.warning("多模态 AI 调用全部失败，降级为纯文本图片描述重试: %s", last_error)
        if text_fallback_messages is not None:
            text_messages = list(text_fallback_messages)
        else:
            text_messages = _strip_image_content(messages)
        response, last_error = await _try_ai_services(text_messages, user_id, tool_context)
        if response is not None:
            return response

    logging.error("所有AI服务均调用失败: %s", last_error)
    return (
        "抱歉喵，雾萌娘在处理你的请求时遇到了一点小问题！现在有点不舒服啦，请稍后再试吧～\n"
        "请联系管理员 @ScarletKc 反馈问题。",
        [],
    )
