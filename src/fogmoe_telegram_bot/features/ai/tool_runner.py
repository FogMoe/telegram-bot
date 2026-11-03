"""工具循环：问模型 → 解析 tool_calls → 执行工具 → 回填结果 → 下一轮，直到模型给出不带工具调用的回复。

这里只放循环骨架与模型调用（含工具执行后的重试）。细节在同目录的模块里：

- `tool_loop_messages.py`：模型返回的 assistant 消息与 tool_calls 转成普通 dict，以及 provider 的协议兼容；
- `tool_loop_execution.py`：一次工具调用的参数校验、执行、日志与指标；
- `tool_loop_delivery.py`：可见内容与生成媒体立刻发到 Telegram，交回模型的公开工具结果；
- `tool_loop_deadline.py`：整轮截止时间在工具中途到期时，给这一轮补齐结果，保持历史配对。
"""

import asyncio
import json
import logging
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from fogmoe_telegram_bot.core import config
from fogmoe_telegram_bot.core.deadline import Deadline, DeadlineExceeded

from .errors import is_retryable_completion_error
from .litellm_client import create_chat_completion
from .prompts import compose_system_prompt
from .tool_loop_deadline import (
    close_interrupted_round,
    deadline_guard,
    log_round_context,
    tool_result_log,
)
from .tool_loop_delivery import (
    close_round_after_media_deadline,
    context_messages_from_tool_result,
    emit_visible_content,
    public_tool_result,
    return_final_text_response,
    send_media_result_immediately,
)
from .tool_loop_execution import execute_tool_call, parse_tool_arguments
from .tool_loop_messages import (
    assistant_message_to_plain,
    normalise_tool_calls,
    resolve_assistant_message,
)
from .tools import AI_TOOL_HANDLERS, OPENAI_TOOLS
from .types import (
    AIResponse,
    PartialAIResponseError,
    ToolLog,
    TurnDeadlineError,
    VisibleContentHandler,
    raise_if_aborted,
)

POST_TOOL_COMPLETION_RETRY_DELAYS_SECONDS = (1.0, 3.0)


def _has_tool_result(tool_logs: list[ToolLog]) -> bool:
    return any(log.get("type") == "tool_result" for log in tool_logs)


async def _create_chat_completion_with_post_tool_retries(
    provider: str,
    model: str,
    *,
    messages: list[dict[str, Any]],
    request_kwargs: dict[str, Any],
    provider_name: str,
    tool_logs: list[ToolLog],
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, object] | None = None,
):
    call_kwargs = dict(request_kwargs)
    if tools is not None:
        call_kwargs["tools"] = tools
    if tool_choice is not None:
        call_kwargs["tool_choice"] = tool_choice

    retry_delays = (
        POST_TOOL_COMPLETION_RETRY_DELAYS_SECONDS
        if _has_tool_result(tool_logs)
        else ()
    )
    retry_index = 0

    while True:
        try:
            return await create_chat_completion(
                provider,
                model,
                messages=messages,
                **call_kwargs,
            )
        except Exception as exc:
            if (
                retry_index >= len(retry_delays)
                or not is_retryable_completion_error(exc)
            ):
                raise

            delay = retry_delays[retry_index]
            retry_index += 1
            logging.warning(
                "%s 工具执行后的回复生成遇到临时错误，%.1f 秒后进行第 %s/%s 次重试: %s",
                provider_name,
                delay,
                retry_index,
                len(retry_delays),
                exc,
            )
            await asyncio.sleep(delay)


async def run_tool_loop(
    provider: str,
    model: str,
    messages: list[dict[str, Any]],
    tool_context: dict[str, object] | None = None,
    *,
    provider_name: str = "AI",
    tool_choice: str | dict[str, object] = "auto",
    context_hard_limit_ratio: float | None = None,
    max_iterations: int = 10,
    completion_timeout: int | None = None,
    skip_tools: Iterable[str] | None = None,
    completion_kwargs: dict[str, Any] | None = None,
    visible_content_handler: VisibleContentHandler | None = None,
    tool_definitions: list[dict[str, Any]] | None = None,
    tool_handlers: Mapping[str, Callable[..., Any]] | None = None,
    system_prompt_override: str | None = None,
    deadline: Deadline | None = None,
) -> AIResponse:
    """Run a tool loop, optionally replacing its advertised tools and handlers.

    原生 async：模型调用 `await`，async 工具直接 `await`，同步工具走有界线程适配器。
    `deadline` 到期时取消正在等待的步骤，抛 `TurnDeadlineError`（带着已有的工具日志）。
    """
    tools = OPENAI_TOOLS if tool_definitions is None else list(tool_definitions)
    handlers = AI_TOOL_HANDLERS if tool_handlers is None else dict(tool_handlers)
    available_tool_names = {
        str((tool.get("function") or {}).get("name"))
        for tool in tools
        if isinstance(tool, dict) and (tool.get("function") or {}).get("name")
    }
    system_message = {
        "role": "system",
        "content": (
            compose_system_prompt(tool_context)
            if system_prompt_override is None
            else system_prompt_override
        ),
    }

    filtered_messages = [
        msg for msg in messages if msg.get("content") is not None or msg.get("tool_calls")
    ]
    filtered_messages.insert(0, system_message)

    tool_logs: list[ToolLog] = []
    skip_set = set(skip_tools or [])
    request_timeout: float | None = (
        config.AI_CHAT_COMPLETION_TIMEOUT_SECONDS
        if completion_timeout is None
        else completion_timeout
    )

    async def complete(
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, object] | None = None,
    ) -> Any:
        """一次模型调用：整轮截止时间到期就取消，单次超时收紧到剩余时间之内。"""
        call_timeout = request_timeout if deadline is None else deadline.clip(request_timeout)
        request_kwargs = {
            **(completion_kwargs or {}),
            "context_hard_limit_ratio": context_hard_limit_ratio,
            "timeout": call_timeout,
        }
        try:
            async with deadline_guard(deadline):
                return await _create_chat_completion_with_post_tool_retries(
                    provider,
                    model,
                    messages=filtered_messages,
                    request_kwargs=request_kwargs,
                    provider_name=provider_name,
                    tool_logs=tool_logs,
                    tools=tools,
                    tool_choice=tool_choice,
                )
        except DeadlineExceeded:
            raise
        except Exception as exc:
            if tool_logs:
                raise PartialAIResponseError(str(exc), tool_logs) from exc
            raise

    # 到期时正在等待的步骤，决定提示和是否把这次计为 provider 失败。
    phase = "model"
    try:
        for iteration in range(max_iterations):
            raise_if_aborted(tool_context)
            phase = "model"
            if deadline is not None:
                deadline.raise_if_expired()
            response = await complete(tools=tools, tool_choice=tool_choice)

            assistant_message, raw_tool_calls = resolve_assistant_message(
                response,
                provider=provider,
                provider_name=provider_name,
            )
            assistant_content = assistant_message.content or ""

            if not raw_tool_calls:
                logging.info("%s 第 %s 轮：无工具调用，直接返回答案", provider_name, iteration + 1)
                phase = "delivery"
                return await return_final_text_response(
                    content_text=assistant_content,
                    tool_logs=tool_logs,
                    visible_content_handler=visible_content_handler,
                    provider_name=provider_name,
                    deadline=deadline,
                )

            tool_calls = normalise_tool_calls(raw_tool_calls)
            logging.info("%s 第 %s 轮：检测到 %s 个工具调用", provider_name, iteration + 1, len(tool_calls))

            assistant_content_for_model = assistant_content
            if visible_content_handler and assistant_content.strip():
                phase = "delivery"
                visible_result = await emit_visible_content(
                    visible_content_handler,
                    assistant_content,
                    provider_name=provider_name,
                    deadline=deadline,
                )
                if visible_result.content:
                    assistant_content_for_model = visible_result.content
                    tool_logs.append({
                        "type": "assistant_visible",
                        "content": visible_result.content,
                    })
                    if not visible_result.completed:
                        return "", tool_logs
                elif not visible_result.completed:
                    return "", tool_logs

            assistant_model_message = assistant_message_to_plain(
                assistant_message,
                content=assistant_content_for_model,
                tool_calls=tool_calls,
            )
            filtered_messages.append(assistant_model_message)

            assistant_message_logged = False
            round_context_messages: list[dict[str, str]] = []
            for call_index, tool_call in enumerate(tool_calls):
                function_payload = tool_call.get("function") or {}
                function_name = function_payload.get("name")
                if not function_name:
                    logging.warning("%s 返回的工具调用缺少函数名: %s", provider_name, tool_call)
                    continue

                if function_name in skip_set:
                    continue

                # 后台任务被撤销后不再执行剩余的工具。
                raise_if_aborted(tool_context)

                arguments = parse_tool_arguments(
                    function_name,
                    function_payload,
                    provider_name=provider_name,
                )
                function_args = arguments.validated

                tool_call_id = tool_call.get("id")
                tool_log_entry = {
                    "type": "assistant_tool_call",
                    "tool_name": function_name,
                    "arguments": arguments.logged,
                    "tool_call_id": tool_call_id,
                }
                if arguments.validation_error is not None:
                    tool_log_entry["validation_error"] = arguments.validation_error
                if not assistant_message_logged:
                    tool_log_entry["assistant_message"] = assistant_model_message
                    assistant_message_logged = True
                tool_logs.append(tool_log_entry)

                # 只有真正运行处理函数时才会在这里到期；校验失败、未开放、未知工具都直接返回错误结果。
                phase = "tool"
                try:
                    internal_tool_result = await execute_tool_call(
                        function_name,
                        arguments,
                        handlers=handlers,
                        available_tool_names=available_tool_names,
                        provider_name=provider_name,
                        deadline=deadline,
                    )
                except DeadlineExceeded as exc:
                    close_interrupted_round(
                        tool_logs,
                        tool_calls,
                        call_index,
                        skip_set,
                        exc.reason,
                        round_context_messages,
                    )
                    raise

                phase = "delivery"
                try:
                    sent_media_messages = await send_media_result_immediately(
                        visible_content_handler=visible_content_handler,
                        tool_name=function_name,
                        tool_result=internal_tool_result,
                        provider_name=provider_name,
                        deadline=deadline,
                    )
                except DeadlineExceeded as exc:
                    close_round_after_media_deadline(
                        tool_logs,
                        tool_calls,
                        call_index,
                        skip_set,
                        exc.reason,
                        round_context_messages,
                        function_name=function_name,
                        function_args=function_args,
                        tool_call_id=tool_call_id,
                        internal_tool_result=internal_tool_result,
                    )
                    raise

                tool_result = public_tool_result(
                    function_name,
                    internal_tool_result,
                    media_sent=bool(sent_media_messages),
                )

                filtered_messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "name": function_name,
                    "content": json.dumps(tool_result, ensure_ascii=False),
                })
                tool_logs.append(
                    tool_result_log(
                        function_name,
                        function_args,
                        tool_call_id,
                        tool_result,
                        internal_tool_result,
                        sent_media_messages=sent_media_messages,
                    )
                )
                round_context_messages.extend(
                    context_messages_from_tool_result(internal_tool_result)
                )

            filtered_messages.extend(round_context_messages)
            log_round_context(tool_logs, round_context_messages)

        logging.warning("%s 工具调用次数超限（%s轮）", provider_name, max_iterations)
        phase = "model"
        if deadline is not None:
            deadline.raise_if_expired()
        response = await complete()

        assistant_message = response.choices[0].message
        raw_tool_calls = getattr(assistant_message, "tool_calls", None)
        if raw_tool_calls:
            logging.warning(
                "%s 工具调用超限后的最终回复仍包含工具调用，忽略工具调用并使用文本内容。",
                provider_name,
            )
        phase = "delivery"
        return await return_final_text_response(
            content_text=assistant_message.content or "",
            tool_logs=tool_logs,
            visible_content_handler=visible_content_handler,
            provider_name=provider_name,
            deadline=deadline,
        )
    except DeadlineExceeded as exc:
        raise TurnDeadlineError(exc.reason, phase, tool_logs) from exc
