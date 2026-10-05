import asyncio
import base64
import contextlib
import inspect
import json
import logging
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any, NamedTuple

from pydantic import ValidationError

from fogmoe_telegram_bot.core import ai_providers, blocking, config, metrics
from fogmoe_telegram_bot.core.deadline import Deadline, DeadlineExceeded
from fogmoe_telegram_bot.core.redaction import describe_exception, log_exception, redact_text

from .errors import is_retryable_completion_error
from .litellm_client import create_chat_completion
from .prompts import compose_system_prompt
from .tools import AI_TOOL_ARG_MODELS, AI_TOOL_HANDLERS, OPENAI_TOOLS
from .tools.dispatch import is_inline_tool
from .types import (
    MEDIA_DELIVERY_KEY,
    MEDIA_DELIVERY_UNKNOWN,
    TOOL_CONTEXT_MESSAGES_KEY,
    AIResponse,
    PartialAIResponseError,
    ToolLog,
    TurnDeadlineError,
    VisibleContentHandler,
    raise_if_aborted,
)

POST_TOOL_COMPLETION_RETRY_DELAYS_SECONDS = (1.0, 3.0)

logger = logging.getLogger(__name__)


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    for attr in ("model_dump", "dict"):
        if hasattr(value, attr):
            dump_func = getattr(value, attr)
            for kwargs in ({"mode": "json"}, {}, {"by_alias": True}):
                try:
                    dumped = dump_func(**kwargs)
                except TypeError:
                    continue
                except Exception:
                    break
                return _json_safe(dumped)
    return str(value)


def _drop_none_items(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None}


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


def _format_validation_errors(exc: ValidationError) -> list[dict[str, str]]:
    details: list[dict[str, str]] = []
    for error in exc.errors(include_url=False):
        loc = ".".join(str(item) for item in error.get("loc", ())) or "__root__"
        details.append({
            "field": loc,
            "message": str(error.get("msg") or "Invalid value"),
            "type": str(error.get("type") or "validation_error"),
        })
    return details


def _validate_tool_args(
    function_name: str,
    raw_args: Any,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    model = AI_TOOL_ARG_MODELS.get(function_name)
    if model is None:
        if isinstance(raw_args, dict):
            return raw_args, None
        return {}, None

    try:
        validated = model.model_validate(raw_args)
    except ValidationError as exc:
        return {}, {
            "error": "Tool arguments failed validation",
            "details": _format_validation_errors(exc),
        }

    return validated.model_dump(
        mode="json",
        exclude_none=True,
        exclude_unset=True,
    ), None


def _tool_call_to_plain(tool_call: Any) -> dict[str, Any]:
    """Normalize a tool call object into a plain JSON-serializable dict."""
    if isinstance(tool_call, dict):
        plain_call = _json_safe(dict(tool_call))
        function_payload = plain_call.get("function")
        if isinstance(function_payload, dict):
            plain_function = dict(function_payload)
            arguments = plain_function.get("arguments")
            if isinstance(arguments, (dict, list)):
                plain_function["arguments"] = json.dumps(arguments, ensure_ascii=False)
            elif arguments is None:
                plain_function["arguments"] = "{}"
            plain_call["function"] = plain_function
        return _drop_none_items(plain_call)
    plain_call: dict[str, Any] | None = None

    for attr in ("model_dump", "dict"):
        if hasattr(tool_call, attr):
            try:
                plain_call = getattr(tool_call, attr)(mode="json")
            except TypeError:
                try:
                    plain_call = getattr(tool_call, attr)()
                except TypeError:
                    plain_call = getattr(tool_call, attr)(by_alias=True)
            except Exception:
                plain_call = None
            if isinstance(plain_call, dict):
                plain_call = _json_safe(plain_call)
                break

    if not isinstance(plain_call, dict):
        function = getattr(tool_call, "function", None)
        arguments = getattr(function, "arguments", None) if function else None
        if isinstance(arguments, (dict, list)):
            arguments_str = json.dumps(arguments, ensure_ascii=False)
        else:
            arguments_str = arguments if arguments is not None else "{}"

        plain_call = {
            "id": getattr(tool_call, "id", None),
            "type": getattr(tool_call, "type", "function"),
            "function": {
                "name": getattr(function, "name", None) if function else None,
                "arguments": arguments_str,
            },
        }
        provider_specific_fields = getattr(
            tool_call,
            "provider_specific_fields",
            None,
        )
        if provider_specific_fields:
            plain_call["provider_specific_fields"] = _json_safe(provider_specific_fields)
        return _drop_none_items(plain_call)

    function_payload = plain_call.get("function")
    if not isinstance(function_payload, dict):
        for attr in ("model_dump", "dict"):
            if hasattr(function_payload, attr):
                try:
                    function_payload = getattr(function_payload, attr)()
                except TypeError:
                    function_payload = getattr(function_payload, attr)(by_alias=True)
                except Exception:
                    function_payload = None
                if isinstance(function_payload, dict):
                    plain_call["function"] = function_payload
                break

    if isinstance(function_payload, dict):
        plain_function = dict(function_payload)
        arguments = plain_function.get("arguments")
        if isinstance(arguments, (dict, list)):
            plain_function["arguments"] = json.dumps(arguments, ensure_ascii=False)
        elif arguments is None:
            plain_function["arguments"] = "{}"
        plain_call["function"] = plain_function

    return _drop_none_items(plain_call)


def _message_to_plain_dict(message: Any) -> dict[str, Any]:
    if isinstance(message, dict):
        return _drop_none_items(_json_safe(dict(message)))

    for attr in ("model_dump", "dict"):
        if hasattr(message, attr):
            dump_func = getattr(message, attr)
            for kwargs in ({"mode": "json"}, {}, {"by_alias": True}):
                try:
                    dumped = dump_func(**kwargs)
                except TypeError:
                    continue
                except Exception:
                    break
                if isinstance(dumped, dict):
                    return _drop_none_items(_json_safe(dumped))

    result: dict[str, Any] = {}
    for key in (
        "role",
        "content",
        "tool_calls",
        "function_call",
        "provider_specific_fields",
        "reasoning_content",
    ):
        value = getattr(message, key, None)
        if value is not None:
            result[key] = _json_safe(value)
    return _drop_none_items(result)


def _assistant_message_to_plain(
    assistant_message: Any,
    *,
    content: str,
    tool_calls: list[dict[str, Any]],
) -> dict[str, Any]:
    message = _message_to_plain_dict(assistant_message)
    message["role"] = "assistant"
    message["content"] = content
    if tool_calls:
        message["tool_calls"] = tool_calls
    else:
        message.pop("tool_calls", None)
    return message


def _normalise_tool_calls(tool_calls: list[Any] | None) -> list[dict[str, Any]]:
    if not tool_calls:
        return []
    return [_tool_call_to_plain(call) for call in tool_calls]


def _resolve_assistant_message(
    response: Any,
    *,
    provider: str,
    provider_name: str,
) -> tuple[Any, list[Any] | None]:
    choices = response.choices
    assistant_message = choices[0].message
    raw_tool_calls = getattr(assistant_message, "tool_calls", None)
    provider_spec = ai_providers.lookup(provider)
    if raw_tool_calls or provider_spec is None or not provider_spec.merges_split_tool_call_choices:
        return assistant_message, raw_tool_calls

    # Temporary compatibility for BerriAI/litellm#35444. Remove after the
    # deployed LiteLLM release merges mixed Responses output into one choice.
    for choice_index, choice in enumerate(choices[1:], start=1):
        candidate_message = getattr(choice, "message", None)
        candidate_tool_calls = getattr(candidate_message, "tool_calls", None)
        if candidate_tool_calls:
            logging.warning(
                "%s 返回的文本和工具调用被拆分到不同 choices；保留 choice 0 文本并使用 choice %s 的工具调用。",
                provider_name,
                choice_index,
            )
            return assistant_message, candidate_tool_calls

    return assistant_message, raw_tool_calls


def _public_tool_result(
    tool_name: str,
    tool_result: dict[str, Any],
    *,
    media_sent: bool = False,
) -> dict[str, Any]:
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


def _context_messages_from_tool_result(tool_result: Any) -> list[dict[str, str]]:
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


def _log_generate_image_result(provider_name: str, tool_result: dict[str, Any]) -> None:
    if not isinstance(tool_result, dict):
        logging.warning("%s generate_image returned non-dict result: %s", provider_name, type(tool_result).__name__)
        return

    if tool_result.get("error"):
        logging.warning(
            "%s generate_image returned error: error=%s, status_code=%s, retry_after_seconds=%s, details=%s",
            provider_name,
            tool_result.get("error"),
            tool_result.get("status_code"),
            tool_result.get("retry_after_seconds"),
            str(tool_result.get("details") or tool_result.get("response_preview") or "")[:500],
        )
        return

    if tool_result.get("status") == "generated":
        images = [tool_result["image"]] if isinstance(tool_result.get("image"), dict) else []
        if not images and isinstance(tool_result.get("images"), list):
            images = tool_result["images"]
        logging.info(
            "%s generate_image generated %s image(s): count=%s, warnings=%s",
            provider_name,
            len(images),
            tool_result.get("count"),
            tool_result.get("warnings"),
        )
        return

    logging.info(
        "%s generate_image returned status=%s",
        provider_name,
        tool_result.get("status") or "unknown",
    )


def _log_generate_voice_result(provider_name: str, tool_result: dict[str, Any]) -> None:
    if not isinstance(tool_result, dict):
        logging.warning("%s generate_voice returned non-dict result: %s", provider_name, type(tool_result).__name__)
        return

    if tool_result.get("error"):
        logging.warning(
            "%s generate_voice returned error: error=%s, status_code=%s, retry_after_seconds=%s, details=%s",
            provider_name,
            tool_result.get("error"),
            tool_result.get("status_code"),
            tool_result.get("retry_after_seconds"),
            str(tool_result.get("details") or tool_result.get("response_preview") or "")[:500],
        )
        return

    if tool_result.get("status") == "generated":
        audios = tool_result.get("audios") if isinstance(tool_result.get("audios"), list) else []
        logging.info(
            "%s generate_voice generated %s audio clip(s): count=%s, warnings=%s",
            provider_name,
            len(audios),
            tool_result.get("count"),
            tool_result.get("warnings"),
        )
        return

    logging.info(
        "%s generate_voice returned status=%s",
        provider_name,
        tool_result.get("status") or "unknown",
    )


def _deadline_guard(deadline: Deadline | None) -> Any:
    """没有截止时间时是空的异步上下文管理器。"""
    return contextlib.nullcontext() if deadline is None else deadline.guard()


async def _send_media_result_immediately(
    *,
    visible_content_handler: VisibleContentHandler | None,
    tool_name: str,
    tool_result: dict[str, Any],
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
        async with _deadline_guard(deadline):
            sent_messages = await send_tool_media(tool_name, tool_result)
    except DeadlineExceeded:
        raise
    except Exception as exc:
        logging.exception("%s failed to send %s result immediately: %s", provider_name, tool_name, exc)
        return []

    if not isinstance(sent_messages, list):
        return []
    return sent_messages


class _VisibleContentResult(NamedTuple):
    content: str
    completed: bool


def _last_visible_content(handler: VisibleContentHandler) -> str:
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


async def _emit_visible_content(
    handler: VisibleContentHandler,
    content: str,
    *,
    provider_name: str,
    deadline: Deadline | None = None,
) -> _VisibleContentResult:
    """Send visible assistant content through the host app and return what was sent."""
    if not content.strip():
        return _VisibleContentResult("", True)

    try:
        async with _deadline_guard(deadline):
            visible_content = await handler(content)
    except DeadlineExceeded:
        raise
    except Exception as exc:
        logging.exception("%s visible content handler failed: %s", provider_name, exc)
        partial_content = _last_visible_content(handler)
        if partial_content:
            return _VisibleContentResult(partial_content, False)
        return _VisibleContentResult("", True)

    if visible_content is None:
        partial_content = _last_visible_content(handler)
        if partial_content:
            return _VisibleContentResult(partial_content, False)
        return _VisibleContentResult("", True)
    normalized = str(visible_content).strip()
    if not normalized:
        partial_content = _last_visible_content(handler)
        if partial_content:
            return _VisibleContentResult(partial_content, False)
    return _VisibleContentResult(normalized, True)


async def _return_final_text_response(
    *,
    content_text: str,
    tool_logs: list[ToolLog],
    visible_content_handler: VisibleContentHandler | None,
    provider_name: str,
    deadline: Deadline | None = None,
) -> AIResponse:
    if content_text.strip():
        if visible_content_handler:
            visible_result = await _emit_visible_content(
                visible_content_handler,
                content_text,
                provider_name=provider_name,
                deadline=deadline,
            )
            if visible_result.content:
                tool_logs.append({
                    "type": "assistant_visible",
                    "content": visible_result.content,
                })
                return "", tool_logs
            if not visible_result.completed:
                return "", tool_logs
            return content_text, tool_logs
        return content_text, tool_logs
    if tool_logs:
        logging.warning("%s 工具调用后最终回复为空。", provider_name)
    return content_text, tool_logs


_INTERRUPTED_TOOL_RESULT = {
    "error": "interrupted",
    "outcome": "unknown",
    "message": (
        "The turn was stopped while this tool was running. It may or may not have "
        "completed; do not assume it failed and do not repeat it without checking."
    ),
}
_NOT_EXECUTED_TOOL_RESULT = {
    "error": "not_executed",
    "message": "The turn was stopped before this tool call could run.",
}


def _handler_is_async(handler: Callable[..., Any]) -> bool:
    return inspect.iscoroutinefunction(handler) or inspect.iscoroutinefunction(
        getattr(handler, "__call__", None)
    )


async def _call_tool(handler: Callable[..., Any], arguments: dict[str, Any]) -> Any:
    """async 工具直接 await；同步工具走有界的线程适配器（并发有上限，保留 contextvars）；
    标记为内联的纯内存工具直接调用。见 `tools/dispatch.py`。"""
    if _handler_is_async(handler):
        return await handler(**arguments)
    if is_inline_tool(handler):
        result = handler(**arguments)
        return await result if inspect.isawaitable(result) else result
    result = await blocking.tools().run(handler, **arguments)
    if inspect.isawaitable(result):
        result = await result
    return result


def _tool_label(function_name: str, handlers: Mapping[str, Any]) -> str:
    """指标标签只用注册过的工具名，模型编造的名字统一记为 unknown。"""
    return function_name if function_name in handlers else "unknown"


def _record_tool_metrics(label: str, started: float, result: Any) -> None:
    metrics.counter("tool.calls", tool=label).inc()
    metrics.histogram("tool.seconds", tool=label).observe(time.perf_counter() - started)
    if isinstance(result, dict) and result.get("error"):
        metrics.counter("tool.failures", tool=label).inc()


def _close_interrupted_round(
    tool_logs: list[ToolLog],
    tool_calls: list[dict[str, Any]],
    interrupted_index: int,
    skip_set: set[str],
    reason: str,
    round_context_messages: list[dict[str, str]],
    *,
    running: bool = True,
) -> None:
    """截止时间在这一轮工具中途到期：给每个还没有结果的工具调用补一条结果。

    正在运行的那个（`running`）标记为「结果未知」，后面没来得及运行的标记为「未执行」，
    这样历史里 assistant 的 tool_calls 与 tool 结果仍然一一配对。`running=False` 表示
    `interrupted_index` 那个调用还没开始（前一个工具已经有结果，是在投递它的媒体时到期的）。
    这一轮已经完成的工具带回来的 Telegram 事件（例如代执行命令的回复）照常记进日志。
    """
    for index, tool_call in enumerate(tool_calls):
        if index < interrupted_index:
            continue
        function_payload = tool_call.get("function") or {}
        function_name = function_payload.get("name")
        if not function_name or function_name in skip_set:
            continue
        if index == interrupted_index and running:
            result = {**_INTERRUPTED_TOOL_RESULT, "reason": reason}
        else:
            result = dict(_NOT_EXECUTED_TOOL_RESULT)
        tool_logs.append({
            "type": "tool_result",
            "tool_name": function_name,
            "arguments": {},
            "result": result,
            "tool_call_id": tool_call.get("id"),
        })
    _log_round_context(tool_logs, round_context_messages)


def _log_round_context(
    tool_logs: list[ToolLog],
    round_context_messages: list[dict[str, str]],
) -> None:
    for context_message in round_context_messages:
        tool_logs.append({
            "type": "telegram_event",
            "role": "user",
            "content": context_message["content"],
        })


def _tool_result_log(
    function_name: str,
    function_args: dict[str, Any],
    tool_call_id: Any,
    tool_result: dict[str, Any],
    internal_tool_result: dict[str, Any],
    *,
    sent_media_messages: list[Any],
    media_delivery_unknown: bool = False,
) -> ToolLog:
    tool_log_entry: ToolLog = {
        "type": "tool_result",
        "tool_name": function_name,
        "arguments": function_args,
        "result": tool_result,
        "tool_call_id": tool_call_id,
    }
    if function_name in {"generate_image", "generate_voice"}:
        tool_log_entry["internal_result"] = internal_tool_result
        if sent_media_messages:
            tool_log_entry["media_sent"] = True
            tool_log_entry["sent_message_count"] = len(sent_media_messages)
        if media_delivery_unknown:
            tool_log_entry[MEDIA_DELIVERY_KEY] = MEDIA_DELIVERY_UNKNOWN
    return tool_log_entry


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
            async with _deadline_guard(deadline):
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

            assistant_message, raw_tool_calls = _resolve_assistant_message(
                response,
                provider=provider,
                provider_name=provider_name,
            )
            assistant_content = assistant_message.content or ""

            if not raw_tool_calls:
                logging.info("%s 第 %s 轮：无工具调用，直接返回答案", provider_name, iteration + 1)
                phase = "delivery"
                return await _return_final_text_response(
                    content_text=assistant_content,
                    tool_logs=tool_logs,
                    visible_content_handler=visible_content_handler,
                    provider_name=provider_name,
                    deadline=deadline,
                )

            tool_calls = _normalise_tool_calls(raw_tool_calls)
            logging.info("%s 第 %s 轮：检测到 %s 个工具调用", provider_name, iteration + 1, len(tool_calls))

            assistant_content_for_model = assistant_content
            if visible_content_handler and assistant_content.strip():
                phase = "delivery"
                visible_result = await _emit_visible_content(
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

            assistant_model_message = _assistant_message_to_plain(
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

                raw_args = function_payload.get("arguments") or "{}"
                try:
                    raw_function_args = json.loads(raw_args)
                except json.JSONDecodeError as exc:
                    logging.error("%s 工具参数解析失败: %s", provider_name, exc)
                    raw_function_args = {}

                function_args, validation_error = _validate_tool_args(
                    function_name,
                    raw_function_args,
                )
                logged_args = (
                    function_args
                    if validation_error is None
                    else _json_safe(raw_function_args)
                )

                tool_call_id = tool_call.get("id")
                tool_log_entry = {
                    "type": "assistant_tool_call",
                    "tool_name": function_name,
                    "arguments": logged_args,
                    "tool_call_id": tool_call_id,
                }
                if validation_error is not None:
                    tool_log_entry["validation_error"] = validation_error
                if not assistant_message_logged:
                    tool_log_entry["assistant_message"] = assistant_model_message
                    assistant_message_logged = True
                tool_logs.append(tool_log_entry)

                handler = handlers.get(function_name)
                if validation_error is not None:
                    logging.warning(
                        "%s 工具参数校验失败: %s, args=%s, error=%s",
                        provider_name,
                        function_name,
                        redact_text(
                            json.dumps(_json_safe(raw_function_args), ensure_ascii=False)
                        ),
                        validation_error.get("details"),
                    )
                    internal_tool_result = validation_error
                elif function_name not in available_tool_names:
                    logging.warning(
                        "%s 拒绝未开放的工具调用: %s",
                        provider_name,
                        function_name,
                    )
                    internal_tool_result = {
                        "error": f"Tool is not available in this agent: {function_name}"
                    }
                elif handler:
                    phase = "tool"
                    tool_started = time.perf_counter()
                    tool_label = _tool_label(function_name, handlers)
                    try:
                        if deadline is not None:
                            deadline.raise_if_expired()
                        async with _deadline_guard(deadline):
                            internal_tool_result = await _call_tool(handler, function_args)
                        if isinstance(internal_tool_result, dict) and internal_tool_result.get("error"):
                            logging.warning(
                                "%s 工具返回错误: %s, args=%s, error=%s",
                                provider_name,
                                function_name,
                                redact_text(json.dumps(function_args, ensure_ascii=False)),
                                redact_text(internal_tool_result.get("error")),
                            )
                        else:
                            logging.info(
                                "%s 工具执行成功: %s, args=%s",
                                provider_name,
                                function_name,
                                redact_text(json.dumps(function_args, ensure_ascii=False)),
                            )
                    except DeadlineExceeded as exc:
                        _record_tool_metrics(tool_label, tool_started, {"error": "interrupted"})
                        _close_interrupted_round(
                            tool_logs,
                            tool_calls,
                            call_index,
                            skip_set,
                            exc.reason,
                            round_context_messages,
                        )
                        raise
                    except TypeError as exc:
                        error_ref = log_exception(
                            logger,
                            f"{provider_name} 工具参数错误: {function_name}",
                            exc,
                        )
                        internal_tool_result = {
                            "error": f"参数错误: {describe_exception(exc)} (ref: {error_ref})"
                        }
                    except Exception as exc:
                        error_ref = log_exception(
                            logger,
                            f"{provider_name} 工具执行失败: {function_name}",
                            exc,
                        )
                        internal_tool_result = {
                            "error": f"执行失败: {describe_exception(exc)} (ref: {error_ref})"
                        }
                    _record_tool_metrics(tool_label, tool_started, internal_tool_result)
                else:
                    logging.warning("%s 未知工具: %s", provider_name, function_name)
                    internal_tool_result = {"error": f"未知工具: {function_name}"}

                if function_name == "generate_image":
                    _log_generate_image_result(provider_name, internal_tool_result)
                elif function_name == "generate_voice":
                    _log_generate_voice_result(provider_name, internal_tool_result)

                phase = "delivery"
                try:
                    sent_media_messages = await _send_media_result_immediately(
                        visible_content_handler=visible_content_handler,
                        tool_name=function_name,
                        tool_result=internal_tool_result,
                        provider_name=provider_name,
                        deadline=deadline,
                    )
                except DeadlineExceeded as exc:
                    # 工具已经执行完，是投递它生成的媒体时到期：先记下这次的结果（是否送达未知，
                    # 投递阶段也不会再发），再给后面没来得及运行的调用补「未执行」。下一轮因此知道
                    # 媒体已经生成，历史也仍然配对。
                    tool_logs.append(
                        _tool_result_log(
                            function_name,
                            function_args,
                            tool_call_id,
                            {
                                **_public_tool_result(function_name, internal_tool_result),
                                "message": _MEDIA_DELIVERY_UNKNOWN_MESSAGES[function_name],
                            },
                            internal_tool_result,
                            sent_media_messages=[],
                            media_delivery_unknown=True,
                        )
                    )
                    round_context_messages.extend(
                        _context_messages_from_tool_result(internal_tool_result)
                    )
                    _close_interrupted_round(
                        tool_logs,
                        tool_calls,
                        call_index + 1,
                        skip_set,
                        exc.reason,
                        round_context_messages,
                        running=False,
                    )
                    raise

                tool_result = _public_tool_result(
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
                    _tool_result_log(
                        function_name,
                        function_args,
                        tool_call_id,
                        tool_result,
                        internal_tool_result,
                        sent_media_messages=sent_media_messages,
                    )
                )
                round_context_messages.extend(
                    _context_messages_from_tool_result(internal_tool_result)
                )

            filtered_messages.extend(round_context_messages)
            _log_round_context(tool_logs, round_context_messages)

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
        return await _return_final_text_response(
            content_text=assistant_message.content or "",
            tool_logs=tool_logs,
            visible_content_handler=visible_content_handler,
            provider_name=provider_name,
            deadline=deadline,
        )
    except DeadlineExceeded as exc:
        raise TurnDeadlineError(exc.reason, phase, tool_logs) from exc
