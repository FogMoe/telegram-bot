"""主聊天的入口：把一轮对话交给共用的回退执行器，再把执行结果整理成类型化的 `LoopResult`。

provider 与模型的顺序、跳过、熔断和失败分类在 `task_runner.run_with_fallback`；这里只放主聊天特有的部分：
每次尝试跑一次工具循环（`_run_chat_candidate`），看不了图的服务换成文字历史，以及把截止时间、
工具之后的失败、上下文超限映射成明确的 `LoopStatus` 与固定提示文案。
"""

from __future__ import annotations

import logging
from typing import Any

from fogmoe_telegram_bot.core import metrics
from fogmoe_telegram_bot.core.deadline import REASON_SHUTDOWN, Deadline

from .chat_capabilities import chat_model_for_service, chat_service_supports_vision
from .errors import find_context_budget_error, is_timeout_error
from .message_content import messages_have_images, strip_image_content
from .task_runner import AllProvidersFailedError, Candidate, ProviderCircuit, run_with_fallback
from .tool_runner import run_tool_loop
from .tools import cleanup_linux_sandbox_async, resolve_agent_tools, tool_request_scope
from .types import (
    LoopResult,
    LoopStatus,
    PartialAIResponseError,
    TurnDeadlineError,
    VisibleContentSink,
    delivered_to_user,
)

AI_PROVIDER_CIRCUIT_FAILURE_THRESHOLD = 3
AI_PROVIDER_CIRCUIT_WINDOW_SECONDS = 5 * 60
AI_PROVIDER_CIRCUIT_COOLDOWN_SECONDS = 30 * 60
CHAT_CIRCUIT = ProviderCircuit(
    failure_threshold=AI_PROVIDER_CIRCUIT_FAILURE_THRESHOLD,
    window_seconds=AI_PROVIDER_CIRCUIT_WINDOW_SECONDS,
    cooldown_seconds=AI_PROVIDER_CIRCUIT_COOLDOWN_SECONDS,
)

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

AI_SERVICE_ERROR_MESSAGE = (
    "抱歉喵，雾萌娘在处理你的请求时遇到了一点小问题！现在有点不舒服啦，请稍后再试吧～\n"
    "请联系管理员 @ScarletKc 反馈问题。"
)

CONTEXT_BUDGET_ERROR_MESSAGE = (
    "当前对话内容太多，系统无法继续处理。请使用 /clear 开始新会话后重试。\n"
    "This conversation is too long for the system to continue. "
    "Use /clear to start a new session and try again."
)

# 整轮截止时间到期：剩余工作已被取消。已经扣的硬币不退。
TURN_DEADLINE_ERROR_MESSAGE = (
    "这次处理花的时间太长，已经中止啦。请稍后再试一次吧～\n"
    "This request took too long and was stopped. Please try again in a moment."
)

# 进程正在停止：在途的轮次被取消。
TURN_SHUTDOWN_ERROR_MESSAGE = (
    "雾萌娘正在重启，这次回复被中断了。请稍后重新发送一次～\n"
    "The bot is restarting and this reply was interrupted. "
    "Please send your message again shortly."
)


def runtime_error_cause(message: str) -> str | None:
    """兼容辅助：仅当测试替身未提供 `LoopStatus` 时按固定错误通知文案映射原因。"""
    if message == PARTIAL_AI_RESPONSE_ERROR_MESSAGE:
        return "partial_ai_response_failed"
    if message == AI_SERVICE_ERROR_MESSAGE:
        return "all_ai_services_failed"
    if message == CONTEXT_BUDGET_ERROR_MESSAGE:
        return "context_budget_exceeded"
    if message == TURN_DEADLINE_ERROR_MESSAGE:
        return "turn_deadline_exceeded"
    if message == TURN_SHUTDOWN_ERROR_MESSAGE:
        return "turn_interrupted_by_shutdown"
    return None


def _messages_for_service(
    service_name: str,
    messages: list[dict[str, Any]],
    text_fallback_messages: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if not messages_have_images(messages):
        return messages
    if chat_service_supports_vision(service_name):
        return messages

    model = chat_model_for_service(service_name)
    logging.info(
        "AI chat provider %s model=%s is configured as text-only; using vision text fallback",
        service_name,
        model,
    )
    if text_fallback_messages is not None:
        return list(text_fallback_messages)
    return strip_image_content(messages)


async def _cleanup_sandbox(provider_name: str) -> None:
    """清理这次尝试开的沙箱。清理失败不影响这次尝试的结果，否则会被当成 provider 失败而换下一个重跑。"""
    try:
        await cleanup_linux_sandbox_async()
    except Exception:
        logging.exception("%s sandbox cleanup failed", provider_name)


async def _run_chat_candidate(
    candidate: Candidate,
    *,
    messages: list[dict[str, Any]],
    text_fallback_messages: list[dict[str, Any]] | None,
    tool_context: dict[str, object] | None,
    request_context: dict[str, object],
    visible_content_handler: VisibleContentSink | None,
    deadline: Deadline | None,
) -> LoopResult:
    """用一个 provider 的一个模型跑一次主聊天的工具循环；provider 之间的差异只来自声明。"""
    provider_name = candidate.spec.display_name
    try:
        service_messages = _messages_for_service(
            candidate.provider,
            messages,
            text_fallback_messages,
        )
        tool_definitions = resolve_agent_tools("main", provider=candidate.spec)
        with tool_request_scope(request_context):
            try:
                raw_result = await run_tool_loop(
                    candidate.provider,
                    candidate.model,
                    list(service_messages),
                    tool_context,
                    provider_name=provider_name,
                    skip_tools=candidate.spec.skip_tools,
                    tool_definitions=tool_definitions,
                    visible_content_handler=visible_content_handler,
                    deadline=deadline,
                )
                return LoopResult.coerce(raw_result, classify_text=runtime_error_cause)
            finally:
                await _cleanup_sandbox(provider_name)
    except TurnDeadlineError as exc:
        logging.warning(
            "%s turn stopped during %s (%s); not retrying",
            provider_name,
            exc.phase,
            exc.reason,
        )
        if exc.phase == "model" and exc.reason != REASON_SHUTDOWN:
            # 正在等待模型响应时到期：对这个 provider 来说就是一次超时。
            CHAT_CIRCUIT.record_failure(candidate.provider)
        raise
    except PartialAIResponseError as exc:
        if find_context_budget_error(exc) is None:
            if is_timeout_error(exc):
                logging.warning(
                    "%s timed out after partial AI response; not retrying",
                    provider_name,
                )
            else:
                logging.error(
                    "%s failed after partial AI response; not retrying: %s",
                    provider_name,
                    exc,
                    exc_info=True,
                )
        raise


def _deadline_response(exc: TurnDeadlineError) -> LoopResult:
    """整轮截止时间到期（或进程停止）：保留已有的工具日志与消息，返回对应状态与提示文案。"""
    metrics.counter("turn.deadline_hits", reason=exc.reason, phase=exc.phase).inc()
    is_shutdown = exc.reason == REASON_SHUTDOWN
    message = TURN_SHUTDOWN_ERROR_MESSAGE if is_shutdown else TURN_DEADLINE_ERROR_MESSAGE
    status = LoopStatus.SHUTDOWN if is_shutdown else LoopStatus.DEADLINE_EXCEEDED
    return LoopResult(
        text=message,
        tool_logs=exc.tool_logs,
        status=status,
        turn_messages=exc.turn_messages,
        error=exc,
    )


def _partial_response(exc: PartialAIResponseError) -> LoopResult:
    """执行过工具或发出过可见内容之后失败：不重试，已有的工具日志与消息照常交回去写历史。"""
    context_error = find_context_budget_error(exc)
    if context_error is not None:
        logging.warning("AI 请求超过上下文硬上限: %s", context_error)
        return LoopResult(
            text=CONTEXT_BUDGET_ERROR_MESSAGE,
            tool_logs=exc.tool_logs,
            status=LoopStatus.CONTEXT_BUDGET_EXCEEDED,
            turn_messages=exc.turn_messages,
            error=exc,
        )
    if delivered_to_user(exc.tool_logs):
        return LoopResult(
            text="",
            tool_logs=exc.tool_logs,
            status=LoopStatus.PARTIAL_FAILURE,
            turn_messages=exc.turn_messages,
            error=exc,
        )
    return LoopResult(
        text=PARTIAL_AI_RESPONSE_ERROR_MESSAGE,
        tool_logs=exc.tool_logs,
        status=LoopStatus.PARTIAL_FAILURE,
        turn_messages=exc.turn_messages,
        error=exc,
    )


async def _try_ai_services(
    messages: list[dict[str, Any]],
    user_id: int,
    tool_context: dict[str, object] | None = None,
    visible_content_handler: VisibleContentSink | None = None,
    text_fallback_messages: list[dict[str, Any]] | None = None,
    deadline: Deadline | None = None,
) -> tuple[LoopResult | None, Exception | None]:
    request_context = dict(tool_context or {})
    request_context.setdefault("user_id", user_id)

    async def attempt(candidate: Candidate) -> LoopResult:
        return await _run_chat_candidate(
            candidate,
            messages=messages,
            text_fallback_messages=text_fallback_messages,
            tool_context=tool_context,
            request_context=request_context,
            visible_content_handler=visible_content_handler,
            deadline=deadline,
        )

    try:
        # 回退链走到一半时间已经用完：不再尝试后面的模型与 provider（包括要跳过的）。
        response = await run_with_fallback(
            "chat",
            attempt,
            circuit=CHAT_CIRCUIT,
            deadline=deadline,
        )
    except AllProvidersFailedError as exc:
        return None, exc.last_error
    except TurnDeadlineError as exc:
        return _deadline_response(exc), None
    except PartialAIResponseError as exc:
        return _partial_response(exc), None
    except Exception as exc:
        context_error = find_context_budget_error(exc)
        if context_error is None:
            raise
        logging.warning("AI 请求超过上下文硬上限: %s", context_error)
        return (
            LoopResult(
                text=CONTEXT_BUDGET_ERROR_MESSAGE,
                tool_logs=[],
                status=LoopStatus.CONTEXT_BUDGET_EXCEEDED,
                turn_messages=[],
                error=exc,
            ),
            None,
        )
    return LoopResult.coerce(response, classify_text=runtime_error_cause), None


async def get_ai_response(
    messages: list[dict[str, Any]],
    user_id: int,
    tool_context: dict[str, object] | None = None,
    text_fallback_messages: list[dict[str, Any]] | None = None,
    visible_content_handler: VisibleContentSink | None = None,
    deadline: Deadline | None = None,
) -> LoopResult:
    """统一 AI 响应异步接口，返回类型化 `LoopResult`（同时兼容 `(text, tool_logs)` 解包）。"""
    response, last_error = await _try_ai_services(
        messages,
        user_id,
        tool_context,
        visible_content_handler,
        text_fallback_messages,
        deadline,
    )
    if response is not None:
        return LoopResult.coerce(response, classify_text=runtime_error_cause)

    if messages_have_images(messages):
        logging.warning("多模态 AI 调用全部失败，降级为纯文本图片描述重试: %s", last_error)
        if text_fallback_messages is not None:
            text_messages = list(text_fallback_messages)
        else:
            text_messages = strip_image_content(messages)
        response, last_error = await _try_ai_services(
            text_messages,
            user_id,
            tool_context,
            visible_content_handler,
            None,
            deadline,
        )
        if response is not None:
            return LoopResult.coerce(response, classify_text=runtime_error_cause)

    logging.error("所有AI服务均调用失败: %s", last_error)
    return LoopResult(
        text=AI_SERVICE_ERROR_MESSAGE,
        tool_logs=[],
        status=LoopStatus.ALL_PROVIDERS_FAILED,
        turn_messages=[],
        error=last_error,
    )
