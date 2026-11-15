"""工具循环的投递：可见内容与生成的媒体立刻发到 Telegram，以及交回模型的工具结果。

- 可见内容：模型在调用工具前后说的话经 `VisibleContentHandler` 立刻发出，状态里记下这次调用实际发出的内容。
- 媒体：声明了 `media` 的工具（`ToolSpec.media`）生成后立刻发出；投递时整轮截止时间到期，这次结果照常记下并
  标记「是否送达未知」，投递阶段不再重发。
- 工具结果分两份：工具返回的内部结果，和交回模型的公开结果（`public_tool_result`）。公开结果去掉附带的
  Telegram 事件；媒体工具只留状态、错误与一句说明，内部结果留在记录的 `internal_result` 里供投递阶段发送。
"""

from __future__ import annotations

import logging
from typing import Any, NamedTuple

from fogmoe_telegram_bot.core.deadline import Deadline, DeadlineExceeded

from .tool_loop_deadline import close_interrupted_round, deadline_guard
from .tool_loop_state import LoopState
from .tools.specs import get_media_tool_spec
from .types import (
    TOOL_CONTEXT_MESSAGES_KEY,
    LoopResult,
    LoopStatus,
    MediaDeliveryState,
    ToolCallStatus,
    VisibleContentHandler,
)


def public_tool_result(
    tool_name: str,
    tool_result: Any,
    *,
    media_sent: bool = False,
) -> Any:
    if isinstance(tool_result, dict) and TOOL_CONTEXT_MESSAGES_KEY in tool_result:
        tool_result = dict(tool_result)
        tool_result.pop(TOOL_CONTEXT_MESSAGES_KEY, None)

    media_spec = get_media_tool_spec(tool_name)
    if media_spec is None or not isinstance(tool_result, dict):
        return tool_result

    return media_spec.public_result(tool_result, media_sent)


def media_delivery_after_execution(
    tool_name: str,
    internal_result: Any,
    sent_media_messages: list[Any],
) -> MediaDeliveryState:
    """工具执行完、即时发送之后这次结果的媒体投递状态。"""
    if get_media_tool_spec(tool_name) is None:
        return MediaDeliveryState.NOT_APPLICABLE
    if sent_media_messages:
        return MediaDeliveryState.SENT
    if isinstance(internal_result, dict) and internal_result.get("status") == "generated":
        return MediaDeliveryState.PENDING
    return MediaDeliveryState.NOT_APPLICABLE


def context_messages_from_tool_result(tool_result: Any) -> list[dict[str, str]]:
    if not isinstance(tool_result, dict):
        return []
    raw_messages = tool_result.get(TOOL_CONTEXT_MESSAGES_KEY)
    if not isinstance(raw_messages, list):
        return []

    return [
        {"role": "user", "content": message}
        for message in raw_messages
        if isinstance(message, str) and message.strip()
    ]


async def send_media_result_immediately(
    *,
    visible_content_handler: VisibleContentHandler | None,
    tool_name: str,
    tool_result: Any,
    provider_name: str,
    deadline: Deadline | None = None,
) -> list[Any]:
    if visible_content_handler is None:
        return []
    if get_media_tool_spec(tool_name) is None:
        return []
    if not isinstance(tool_result, dict) or tool_result.get("status") != "generated":
        return []

    try:
        async with deadline_guard(deadline):
            return await visible_content_handler.send_tool_media(tool_name, tool_result)
    except DeadlineExceeded:
        raise
    except Exception as exc:
        logging.exception("%s failed to send %s result immediately: %s", provider_name, tool_name, exc)
        return []


def close_round_after_media_deadline(
    state: LoopState,
    record_index: int,
    tool_calls: list[dict[str, Any]],
    call_index: int,
    reason: str,
    round_context_messages: list[dict[str, str]],
    *,
    internal_tool_result: dict[str, Any],
) -> None:
    """`call_index` 的工具已经执行完，是投递它生成的媒体时到期。

    先记下这次的结果（是否送达未知，投递阶段也不会再发），再给后面没来得及运行的调用补「未执行」。
    下一轮因此知道媒体已经生成，历史也仍然配对。只有声明了 `media` 的工具会走到这里。
    """
    record = state.tool_calls[record_index]
    media_spec = get_media_tool_spec(record.tool_name)
    if media_spec is None:
        raise RuntimeError(f"{record.tool_name} is not a media tool")
    state.finish_tool_call(
        record_index,
        status=ToolCallStatus.COMPLETED,
        public_result={
            **public_tool_result(record.tool_name, internal_tool_result),
            "message": media_spec.delivery_unknown_message,
        },
        internal_result=internal_tool_result,
        media_delivery=MediaDeliveryState.UNKNOWN,
    )
    round_context_messages.extend(context_messages_from_tool_result(internal_tool_result))
    close_interrupted_round(
        state,
        tool_calls,
        call_index + 1,
        reason,
        round_context_messages,
    )


class VisibleContentResult(NamedTuple):
    """一次可见发送的结果：`content` 是这次实际发出的文字（没发出任何内容时为空），
    `completed` 为 False 表示发送中途失败、只发出了 `content` 这一部分。"""

    content: str
    completed: bool


def _content_sent_since(handler: VisibleContentHandler, sent_before: int) -> str:
    """这次调用期间实际发出的文字（`sent_contents` 里 `sent_before` 之后的部分）。"""
    for content in reversed(handler.sent_contents[sent_before:]):
        normalized = str(content or "").strip()
        if normalized:
            return normalized
    return ""


async def emit_visible_content(
    handler: VisibleContentHandler,
    content: str,
    *,
    provider_name: str,
    deadline: Deadline | None = None,
) -> VisibleContentResult:
    """Send visible assistant content through the host app and return what this call sent."""
    if not content.strip():
        return VisibleContentResult("", True)

    sent_before = len(handler.sent_contents)
    try:
        async with deadline_guard(deadline):
            visible_content = await handler(content)
    except DeadlineExceeded:
        raise
    except Exception as exc:
        logging.exception("%s visible content handler failed: %s", provider_name, exc)
        partial_content = _content_sent_since(handler, sent_before)
        if partial_content:
            return VisibleContentResult(partial_content, False)
        return VisibleContentResult("", True)

    return VisibleContentResult(visible_content.strip(), True)


async def return_final_text_response(
    state: LoopState,
    *,
    content_text: str,
    visible_content_handler: VisibleContentHandler | None,
    provider_name: str,
    deadline: Deadline | None = None,
    status: LoopStatus = LoopStatus.COMPLETED,
) -> LoopResult:
    if content_text.strip() and visible_content_handler is not None:
        visible_result = await emit_visible_content(
            visible_content_handler,
            content_text,
            provider_name=provider_name,
            deadline=deadline,
        )
        if visible_result.content:
            state.record_delivered(visible_result.content, append_message=True)
            return state.result("", status)
    elif not content_text.strip() and state.tool_calls:
        logging.warning("%s 工具调用后最终回复为空。", provider_name)
    return state.result(content_text, status)
