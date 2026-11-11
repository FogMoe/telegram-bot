"""工具循环的投递：可见内容与生成的媒体立刻发到 Telegram，以及交回模型的工具结果。

- 可见内容：模型在调用工具前后说的话经 `VisibleContentHandler` 立刻发出，日志里记下实际发出的内容。
- 媒体：`generate_image` / `generate_voice` 生成后立刻发出；投递时整轮截止时间到期，这次结果照常记下并标记
  「是否送达未知」，投递阶段不再重发。
- 工具结果分两份：工具返回的内部结果，和交回模型的公开结果（`public_tool_result`）。公开结果去掉附带的
  Telegram 事件；媒体工具只留状态、错误与一句说明，内部结果记在日志的 `internal_result` 里供投递阶段发送。
"""

from __future__ import annotations

import logging
from typing import Any, NamedTuple

from fogmoe_telegram_bot.core.deadline import Deadline, DeadlineExceeded

from .tool_loop_deadline import close_interrupted_round, deadline_guard, tool_result_log
from .tool_loop_messages import tool_result_json
from .types import (
    TOOL_CONTEXT_MESSAGES_KEY,
    LoopResult,
    LoopStatus,
    ToolLog,
    VisibleContentSink,
)

_MEDIA_DELIVERY_UNKNOWN_MESSAGES = {
    "generate_image": (
        "The image was generated, but the turn was stopped while it was being sent to "
        "Telegram; it may or may not have arrived. Do not generate it again unless the "
        "user asks."
    ),
    "generate_voice": (
        "The audio was generated, but the turn was stopped while it was being sent to "
        "Telegram; it may or may not have arrived. Do not generate it again unless the "
        "user asks."
    ),
}


def public_tool_result(
    tool_name: str,
    tool_result: Any,
    *,
    media_sent: bool = False,
) -> Any:
    if isinstance(tool_result, dict) and TOOL_CONTEXT_MESSAGES_KEY in tool_result:
        tool_result = dict(tool_result)
        tool_result.pop(TOOL_CONTEXT_MESSAGES_KEY, None)

    if tool_name not in {"generate_image", "generate_voice"} or not isinstance(tool_result, dict):
        return tool_result

    if tool_result.get("error"):
        public_result = {"error": tool_result.get("error")}
        for key in ("status_code", "details", "response_preview", "warnings", "retry_after_seconds"):
            if key in tool_result:
                public_result[key] = tool_result[key]
        return public_result

    if tool_name == "generate_image" and tool_result.get("status") == "generated":
        return {
            "status": "generated",
            "message": (
                "Generated image has been sent to Telegram."
                if media_sent
                else (
                    "Generated image is ready and will be sent to Telegram. "
                    "If you need to inspect the image yourself later, ask the user "
                    "to forward the sent image back to you."
                )
            ),
        }

    if tool_name == "generate_voice" and tool_result.get("status") == "generated":
        return {
            "status": "generated",
            "message": (
                "Generated audio has been sent to Telegram."
                if media_sent
                else "Generated audio is ready and will be sent to Telegram."
            ),
        }

    return {"status": tool_result.get("status") or "unknown"}


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
    visible_content_handler: VisibleContentSink | None,
    tool_name: str,
    tool_result: Any,
    provider_name: str,
    deadline: Deadline | None = None,
) -> list[Any]:
    if visible_content_handler is None:
        return []
    if tool_name not in {"generate_image", "generate_voice"}:
        return []
    if not isinstance(tool_result, dict) or tool_result.get("status") != "generated":
        return []

    send_tool_media = getattr(visible_content_handler, "send_tool_media", None)
    if not callable(send_tool_media):
        return []

    try:
        async with deadline_guard(deadline):
            sent_messages = await send_tool_media(tool_name, tool_result)
    except DeadlineExceeded:
        raise
    except Exception as exc:
        logging.exception("%s failed to send %s result immediately: %s", provider_name, tool_name, exc)
        return []

    if not isinstance(sent_messages, list):
        return []
    return sent_messages


def close_round_after_media_deadline(
    tool_logs: list[ToolLog],
    tool_calls: list[dict[str, Any]],
    call_index: int,
    skip_set: set[str],
    reason: str,
    round_context_messages: list[dict[str, str]],
    *,
    function_name: str,
    function_args: dict[str, Any],
    tool_call_id: Any,
    internal_tool_result: dict[str, Any],
    filtered_messages: list[dict[str, Any]] | None = None,
) -> None:
    """`call_index` 的工具已经执行完，是投递它生成的媒体时到期。

    先记下这次的结果（是否送达未知，投递阶段也不会再发），再给后面没来得及运行的调用补「未执行」。
    下一轮因此知道媒体已经生成，历史也仍然配对。
    """
    public_result = {
        **public_tool_result(function_name, internal_tool_result),
        "message": _MEDIA_DELIVERY_UNKNOWN_MESSAGES[function_name],
    }
    if filtered_messages is not None:
        filtered_messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "name": function_name,
                "content": tool_result_json(public_result),
            }
        )
    tool_logs.append(
        tool_result_log(
            function_name,
            function_args,
            tool_call_id,
            public_result,
            internal_tool_result,
            sent_media_messages=[],
            media_delivery_unknown=True,
        )
    )
    round_context_messages.extend(
        context_messages_from_tool_result(internal_tool_result)
    )
    close_interrupted_round(
        tool_logs,
        tool_calls,
        call_index + 1,
        skip_set,
        reason,
        round_context_messages,
        running=False,
        filtered_messages=filtered_messages,
    )


class VisibleContentResult(NamedTuple):
    content: str
    completed: bool


def _last_visible_content(handler: VisibleContentSink) -> str:
    visible_events = getattr(handler, "visible_events", None)
    if callable(visible_events):
        try:
            events = visible_events()
            if isinstance(events, list):
                for event in reversed(events):
                    if not isinstance(event, dict):
                        continue
                    content = str(event.get("content") or "").strip()
                    if content:
                        return content
        except Exception:
            logging.exception("Failed to read visible content events")

    contents = getattr(handler, "sent_contents", [])
    if not isinstance(contents, list):
        return ""
    for content in reversed(contents):
        normalized = str(content or "").strip()
        if normalized:
            return normalized
    return ""


async def emit_visible_content(
    handler: VisibleContentSink,
    content: str,
    *,
    provider_name: str,
    deadline: Deadline | None = None,
) -> VisibleContentResult:
    """Send visible assistant content through the host app and return what was sent."""
    if not content.strip():
        return VisibleContentResult("", True)

    try:
        async with deadline_guard(deadline):
            visible_content = await handler(content)
    except DeadlineExceeded:
        raise
    except Exception as exc:
        logging.exception("%s visible content handler failed: %s", provider_name, exc)
        partial_content = _last_visible_content(handler)
        if partial_content:
            return VisibleContentResult(partial_content, False)
        return VisibleContentResult("", True)

    if visible_content is None:
        partial_content = _last_visible_content(handler)
        if partial_content:
            return VisibleContentResult(partial_content, False)
        return VisibleContentResult("", True)
    normalized = str(visible_content).strip()
    if not normalized:
        partial_content = _last_visible_content(handler)
        if partial_content:
            return VisibleContentResult(partial_content, False)
    return VisibleContentResult(normalized, True)


async def return_final_text_response(
    *,
    content_text: str,
    tool_logs: list[ToolLog],
    visible_content_handler: VisibleContentSink | None,
    provider_name: str,
    deadline: Deadline | None = None,
    status: LoopStatus = LoopStatus.COMPLETED,
    filtered_messages: list[dict[str, Any]] | None = None,
    turn_start: int = 0,
) -> LoopResult:
    def snapshot_messages() -> list[dict[str, Any]]:
        if filtered_messages is None:
            return []
        return list(filtered_messages[turn_start:])

    if content_text.strip():
        if visible_content_handler:
            visible_result = await emit_visible_content(
                visible_content_handler,
                content_text,
                provider_name=provider_name,
                deadline=deadline,
            )
            if visible_result.content:
                tool_logs.append(
                    {
                        "type": "assistant_visible",
                        "content": visible_result.content,
                    }
                )
                if filtered_messages is not None:
                    filtered_messages.append(
                        {"role": "assistant", "content": visible_result.content}
                    )
                return LoopResult(
                    text="",
                    tool_logs=tool_logs,
                    status=status,
                    turn_messages=snapshot_messages(),
                )
            if not visible_result.completed:
                return LoopResult(
                    text="",
                    tool_logs=tool_logs,
                    status=status,
                    turn_messages=snapshot_messages(),
                )
            return LoopResult(
                text=content_text,
                tool_logs=tool_logs,
                status=status,
                turn_messages=snapshot_messages(),
            )
        return LoopResult(
            text=content_text,
            tool_logs=tool_logs,
            status=status,
            turn_messages=snapshot_messages(),
        )
    if tool_logs:
        logging.warning("%s 工具调用后最终回复为空。", provider_name)
    return LoopResult(
        text=content_text,
        tool_logs=tool_logs,
        status=status,
        turn_messages=snapshot_messages(),
    )
