import asyncio
import logging
from typing import Any, Dict, Optional

from core import config

from .tools import clear_tool_request_context, set_tool_request_context
from .errors import SafetyBlockError
from .providers import azure, gemini, openai, zhipu
from .runtime import EXECUTOR
from .types import AIResponse, PartialAIResponseError, VisibleContentHandler

AI_SERVICE_MAP = {
    "openai": openai.get_ai_response,
    "gemini": gemini.get_ai_response,
    "azure": azure.get_ai_response,
    "zhipu": zhipu.get_ai_response,
    "zai": zhipu.get_ai_response,
}

AI_SERVICE_ORDER = config.AI_SERVICE_ORDER

PARTIAL_AI_RESPONSE_ERROR_MESSAGE = (
    "看起来对话出现了一些小问题呢。"
    "您可以尝试使用 /clear 命令来清空聊天记录，"
    "然后我们重新开始对话吧！\n"
    "It seems there was a small issue with the conversation."
    "You can try using the /clear command to clear the chat history,"
    "and then we can start over!\n\n"
    "错误信息 Error message: \n\n"
    "问题类型：工具执行后回复生成失败。\n"
    "Issue type: response generation failed after tool execution.\n\n"
    "内部处理失败，详细信息已记录。\n"
    "Internal processing failed. Details have been logged.\n\n"
    "您可以发送给管理员 @ScarletKc 报告此问题。\n"
    "You can report this issue to the admin @ScarletKc."
)


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
    visible_content_handler: Optional[VisibleContentHandler],
) -> AIResponse:
    set_tool_request_context(dict(tool_context or {}))
    try:
        return AI_SERVICE_MAP[service_name](
            messages,
            user_id,
            tool_context,
            visible_content_handler=visible_content_handler,
        )
    finally:
        clear_tool_request_context()


def _visible_content_was_sent(
    visible_content_handler: Optional[VisibleContentHandler],
) -> bool:
    if visible_content_handler is None:
        return False
    try:
        sent_count = int(getattr(visible_content_handler, "sent_count", 0))
    except (TypeError, ValueError):
        sent_count = 0
    if sent_count > 0:
        return True

    contents = getattr(visible_content_handler, "sent_contents", [])
    if isinstance(contents, list) and any(str(content).strip() for content in contents):
        return True

    visible_events = getattr(visible_content_handler, "visible_events", None)
    if callable(visible_events):
        try:
            events = visible_events()
            return isinstance(events, list) and any(
                isinstance(event, dict) and str(event.get("content") or "").strip()
                for event in events
            )
        except Exception:
            logging.exception("Failed to read visible content sent state")
            return False
    return False


def _visible_content_events(
    visible_content_handler: Optional[VisibleContentHandler],
) -> list[dict]:
    if visible_content_handler is None:
        return []
    visible_events = getattr(visible_content_handler, "visible_events", None)
    if callable(visible_events):
        try:
            events = visible_events()
            if isinstance(events, list):
                return events
        except Exception:
            logging.exception("Failed to read visible content events")
    contents = getattr(visible_content_handler, "sent_contents", [])
    if not isinstance(contents, list):
        return []
    return [
        {
            "type": "assistant_visible",
            "content": str(content),
        }
        for content in contents
        if str(content).strip()
    ]


async def _try_ai_services(
    messages,
    user_id: int,
    tool_context: Optional[Dict[str, object]] = None,
    visible_content_handler: Optional[VisibleContentHandler] = None,
) -> tuple[AIResponse | None, Exception | None]:
    last_error = None
    loop = asyncio.get_running_loop()

    for service_name in AI_SERVICE_ORDER:
        try:
            response = await loop.run_in_executor(
                EXECUTOR,
                lambda s=service_name: _call_service_with_context(
                    s,
                    messages.copy(),
                    user_id,
                    tool_context,
                    visible_content_handler,
                ),
            )
            return response, None
        except SafetyBlockError:
            if _visible_content_was_sent(visible_content_handler):
                logging.warning(
                    "%s triggered safety block after sending visible content; not retrying",
                    service_name,
                )
                return ("", _visible_content_events(visible_content_handler)), None
            if service_name == "gemini":
                logging.warning("Gemini triggered safety block, trying next service")
                last_error = SafetyBlockError("SafetyBlockError")
                continue
            raise
        except PartialAIResponseError as exc:
            logging.error(
                "%s failed after partial AI response; not retrying: %s",
                service_name,
                exc,
            )
            if _visible_content_was_sent(visible_content_handler):
                return ("", exc.tool_logs), None
            return (PARTIAL_AI_RESPONSE_ERROR_MESSAGE, exc.tool_logs), None
        except Exception as exc:
            if _visible_content_was_sent(visible_content_handler):
                logging.error(
                    "%s failed after sending visible content; not retrying: %s",
                    service_name,
                    exc,
                )
                return ("", _visible_content_events(visible_content_handler)), None
            logging.warning("%s 调用失败: %s", service_name, exc)
            last_error = exc
            continue

    return None, last_error


async def get_ai_response(
    messages,
    user_id: int,
    tool_context: Optional[Dict[str, object]] = None,
    text_fallback_messages=None,
    visible_content_handler: Optional[VisibleContentHandler] = None,
) -> AIResponse:
    """
    统一AI响应异步接口，根据配置的顺序依次尝试不同的AI服务
    """
    response, last_error = await _try_ai_services(
        messages,
        user_id,
        tool_context,
        visible_content_handler,
    )
    if response is not None:
        return response

    if _messages_have_images(messages):
        logging.warning("多模态 AI 调用全部失败，降级为纯文本图片描述重试: %s", last_error)
        if text_fallback_messages is not None:
            text_messages = list(text_fallback_messages)
        else:
            text_messages = _strip_image_content(messages)
        response, last_error = await _try_ai_services(
            text_messages,
            user_id,
            tool_context,
            visible_content_handler,
        )
        if response is not None:
            return response

    logging.error("所有AI服务均调用失败: %s", last_error)
    return (
        "抱歉喵，雾萌娘在处理你的请求时遇到了一点小问题！现在有点不舒服啦，请稍后再试吧～\n"
        "请联系管理员 @ScarletKc 反馈问题。",
        [],
    )
