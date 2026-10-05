import logging
import time
from typing import Dict, Optional

from fogmoe_telegram_bot.core import ai_providers, metrics
from fogmoe_telegram_bot.core.deadline import REASON_SHUTDOWN, Deadline

from .chat_capabilities import chat_model_for_service, chat_service_supports_vision
from .chat_provider import run_chat_provider
from .context_budget import ContextBudgetExceededError
from .message_content import messages_have_images, strip_image_content
from .provider_resolver import get_provider_order_for_task
from .tools import (
    clear_tool_request_context,
    cleanup_linux_sandbox_async,
    set_tool_request_context,
)
from .errors import SafetyBlockError, is_timeout_error
from .types import (
    AIResponse,
    PartialAIResponseError,
    TurnDeadlineError,
    VisibleContentHandler,
)

AI_PROVIDER_CIRCUIT_FAILURE_THRESHOLD = 3
AI_PROVIDER_CIRCUIT_WINDOW_SECONDS = 5 * 60
AI_PROVIDER_CIRCUIT_COOLDOWN_SECONDS = 30 * 60
_provider_failure_streaks: dict[str, list[float]] = {}
_provider_circuit_open_until: dict[str, float] = {}

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


def _context_budget_error_message(_: ContextBudgetExceededError) -> str:
    return CONTEXT_BUDGET_ERROR_MESSAGE


def _find_context_budget_error(exc: BaseException) -> ContextBudgetExceededError | None:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ContextBudgetExceededError):
            return current
        current = current.__cause__ or current.__context__
    return None


def runtime_error_cause(message: str) -> str | None:
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


def _provider_circuit_is_open(service_name: str, now: float | None = None) -> bool:
    current_time = time.monotonic() if now is None else now
    open_until = _provider_circuit_open_until.get(service_name)
    if not open_until:
        return False
    if current_time < open_until:
        return True

    _provider_circuit_open_until.pop(service_name, None)
    _provider_failure_streaks.pop(service_name, None)
    return False


def _record_provider_success(service_name: str) -> None:
    _provider_failure_streaks.pop(service_name, None)
    _provider_circuit_open_until.pop(service_name, None)


def _record_provider_failure(service_name: str, now: float | None = None) -> None:
    current_time = time.monotonic() if now is None else now
    cutoff = current_time - AI_PROVIDER_CIRCUIT_WINDOW_SECONDS
    recent_failures = [
        failure_time
        for failure_time in _provider_failure_streaks.get(service_name, [])
        if failure_time >= cutoff
    ]
    recent_failures.append(current_time)
    _provider_failure_streaks[service_name] = recent_failures

    if len(recent_failures) >= AI_PROVIDER_CIRCUIT_FAILURE_THRESHOLD:
        open_until = current_time + AI_PROVIDER_CIRCUIT_COOLDOWN_SECONDS
        _provider_circuit_open_until[service_name] = open_until
        _provider_failure_streaks.pop(service_name, None)
        logging.warning(
            "%s 熔断 %s 秒：%s 秒内连续失败 %s 次",
            service_name,
            AI_PROVIDER_CIRCUIT_COOLDOWN_SECONDS,
            AI_PROVIDER_CIRCUIT_WINDOW_SECONDS,
            AI_PROVIDER_CIRCUIT_FAILURE_THRESHOLD,
        )


async def _call_service_with_context(
    service_name: str,
    messages,
    user_id: int,
    tool_context: Optional[Dict[str, object]],
    visible_content_handler: Optional[VisibleContentHandler],
    deadline: Deadline | None = None,
) -> AIResponse:
    request_context = dict(tool_context or {})
    request_context.setdefault("user_id", user_id)
    set_tool_request_context(request_context)
    try:
        return await run_chat_provider(
            service_name,
            messages,
            user_id,
            tool_context,
            visible_content_handler=visible_content_handler,
            deadline=deadline,
        )
    finally:
        try:
            await cleanup_linux_sandbox_async()
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

    sent_messages = getattr(visible_content_handler, "sent_messages", [])
    if isinstance(sent_messages, list) and any(message is not None for message in sent_messages):
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


def _messages_for_service(
    service_name: str,
    messages,
    text_fallback_messages=None,
):
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


def _deadline_response(
    exc: TurnDeadlineError,
    service_name: str,
    visible_content_handler: Optional[VisibleContentHandler],
) -> AIResponse:
    """整轮截止时间到期（或进程停止）：保留已有的工具日志，回复固定的提示文案。

    与 `PartialAIResponseError` 的区别：已经向用户发送过可见内容时仍然给提示，
    因为这一轮没有正常结束，用户需要知道。
    """
    logging.warning(
        "%s turn stopped during %s (%s); not retrying",
        service_name,
        exc.phase,
        exc.reason,
    )
    metrics.counter("turn.deadline_hits", reason=exc.reason, phase=exc.phase).inc()
    if exc.phase == "model" and exc.reason != REASON_SHUTDOWN:
        # 正在等待模型响应时到期：对这个 provider 来说就是一次超时。
        _record_provider_failure(service_name)
    message = (
        TURN_SHUTDOWN_ERROR_MESSAGE
        if exc.reason == REASON_SHUTDOWN
        else TURN_DEADLINE_ERROR_MESSAGE
    )
    return message, exc.tool_logs


async def _try_ai_services(
    messages,
    user_id: int,
    tool_context: Optional[Dict[str, object]] = None,
    visible_content_handler: Optional[VisibleContentHandler] = None,
    text_fallback_messages=None,
    deadline: Deadline | None = None,
) -> tuple[AIResponse | None, Exception | None]:
    last_error = None

    for service_name in get_provider_order_for_task("chat"):
        if deadline is not None and deadline.expired:
            # 回退链走到一半时间已经用完：不再尝试后面的 provider。
            return _deadline_response(
                TurnDeadlineError(deadline.expiry_reason, "fallback", []),
                service_name,
                visible_content_handler,
            ), None

        if _provider_circuit_is_open(service_name):
            logging.warning("%s 当前处于熔断冷却中，跳过调用", service_name)
            continue

        service_messages = _messages_for_service(
            service_name,
            messages,
            text_fallback_messages,
        )
        try:
            response = await _call_service_with_context(
                service_name,
                service_messages.copy(),
                user_id,
                tool_context,
                visible_content_handler,
                deadline,
            )
            _record_provider_success(service_name)
            return response, None
        except TurnDeadlineError as exc:
            return _deadline_response(exc, service_name, visible_content_handler), None
        except SafetyBlockError:
            if _visible_content_was_sent(visible_content_handler):
                logging.warning(
                    "%s triggered safety block after sending visible content; not retrying",
                    service_name,
                )
                return ("", _visible_content_events(visible_content_handler)), None
            provider_spec = ai_providers.lookup(service_name)
            if provider_spec is not None and provider_spec.safety_blocks_fall_through:
                logging.warning(
                    "%s triggered safety block, trying next service",
                    provider_spec.display_name,
                )
                last_error = SafetyBlockError("SafetyBlockError")
                continue
            raise
        except PartialAIResponseError as exc:
            context_error = _find_context_budget_error(exc)
            if context_error is not None:
                logging.warning("AI 请求超过上下文硬上限: %s", context_error)
                return (_context_budget_error_message(context_error), exc.tool_logs), None
            if is_timeout_error(exc):
                logging.warning(
                    "%s timed out after partial AI response; not retrying",
                    service_name,
                )
            else:
                logging.error(
                    "%s failed after partial AI response; not retrying: %s",
                    service_name,
                    exc,
                    exc_info=True,
                )
            if _visible_content_was_sent(visible_content_handler):
                return ("", exc.tool_logs), None
            return (PARTIAL_AI_RESPONSE_ERROR_MESSAGE, exc.tool_logs), None
        except Exception as exc:
            context_error = _find_context_budget_error(exc)
            if context_error is not None:
                logging.warning("AI 请求超过上下文硬上限: %s", context_error)
                return (_context_budget_error_message(context_error), []), None
            if _visible_content_was_sent(visible_content_handler):
                if is_timeout_error(exc):
                    logging.warning(
                        "%s timed out after sending visible content; not retrying",
                        service_name,
                    )
                else:
                    logging.error(
                        "%s failed after sending visible content; not retrying: %s",
                        service_name,
                        exc,
                        exc_info=True,
                    )
                return ("", _visible_content_events(visible_content_handler)), None
            logging.warning("%s 调用失败: %s", service_name, exc)
            _record_provider_failure(service_name)
            last_error = exc
            continue

    return None, last_error


async def get_ai_response(
    messages,
    user_id: int,
    tool_context: Optional[Dict[str, object]] = None,
    text_fallback_messages=None,
    visible_content_handler: Optional[VisibleContentHandler] = None,
    deadline: Deadline | None = None,
) -> AIResponse:
    """
    统一AI响应异步接口，根据配置的顺序依次尝试不同的AI服务

    `deadline`（可选）覆盖 provider 回退、工具与可见内容投递；到期时取消剩余工作并返回
    `TURN_DEADLINE_ERROR_MESSAGE`（进程停止时是 `TURN_SHUTDOWN_ERROR_MESSAGE`）与已有的工具日志。
    """
    response, last_error = await _try_ai_services(
        messages,
        user_id,
        tool_context,
        visible_content_handler,
        text_fallback_messages,
        deadline,
    )
    if response is not None:
        return response

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
            return response

    logging.error("所有AI服务均调用失败: %s", last_error)
    return (AI_SERVICE_ERROR_MESSAGE, [])
