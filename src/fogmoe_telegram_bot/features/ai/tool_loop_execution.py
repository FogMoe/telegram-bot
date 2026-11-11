"""工具循环里的一次工具执行：解析与校验参数、调用处理函数、记录日志与指标。

处理函数的三种执行方式（async、同步走有界线程适配器、内联）见 `tools/dispatch.py`。工具出错不会让这一轮失败：
参数校验失败、工具未开放或未知、处理函数抛异常都变成带 `error` 的结果交回模型。只有整轮截止时间到期
（`DeadlineExceeded`）向上抛，由工具循环给这一轮补齐结果。
"""

import inspect
import json
import logging
import time
from collections.abc import Callable, Mapping
from typing import Any, NamedTuple

from pydantic import ValidationError

from fogmoe_telegram_bot.core import blocking, metrics
from fogmoe_telegram_bot.core.deadline import Deadline, DeadlineExceeded
from fogmoe_telegram_bot.core.redaction import describe_exception, log_exception, redact_text

from .tool_loop_deadline import deadline_guard
from .tool_loop_messages import json_safe
from .tools import AI_TOOL_ARG_MODELS, get_media_tool_spec, get_tool_spec
from .tools.dispatch import is_inline_tool
from .usage import record_tool_execution_usage

logger = logging.getLogger(__name__)


class ToolArguments(NamedTuple):
    """一次工具调用的参数：`raw` 是模型给的 JSON，`validated` 是校验后交给处理函数的参数。"""

    raw: Any
    validated: dict[str, Any]
    validation_error: dict[str, Any] | None

    @property
    def logged(self) -> Any:
        """记进日志的参数：校验通过时是校验后的参数，否则是原始参数。"""
        return self.validated if self.validation_error is None else json_safe(self.raw)


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


def parse_tool_arguments(
    function_name: str,
    function_payload: dict[str, Any],
    *,
    provider_name: str,
) -> ToolArguments:
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
    return ToolArguments(raw_function_args, function_args, validation_error)


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
    failed = bool(isinstance(result, dict) and result.get("error"))
    if failed:
        metrics.counter("tool.failures", tool=label).inc()
    spec = get_tool_spec(label)
    record_tool_execution_usage(
        label,
        billing_category=spec.billing_category if spec is not None else None,
        failed=failed,
    )


async def execute_tool_call(
    function_name: str,
    arguments: ToolArguments,
    *,
    handlers: Mapping[str, Callable[..., Any]],
    available_tool_names: set[str],
    provider_name: str,
    deadline: Deadline | None = None,
) -> Any:
    """执行一个工具调用，返回工具的内部结果。截止时间到期时记一次 interrupted 指标后向上抛。"""
    function_args = arguments.validated
    validation_error = arguments.validation_error
    handler = handlers.get(function_name)
    if not function_name:
        logging.warning("%s 返回的工具调用缺少函数名: args=%s", provider_name, arguments.raw)
        internal_tool_result: Any = {"error": "Tool call is missing function name"}
    elif function_name not in available_tool_names:
        logging.warning(
            "%s 拒绝未开放的工具调用: %s",
            provider_name,
            function_name,
        )
        internal_tool_result = {
            "error": f"Tool is not available in this agent: {function_name}"
        }
    elif validation_error is not None:
        logging.warning(
            "%s 工具参数校验失败: %s, args=%s, error=%s",
            provider_name,
            function_name,
            redact_text(
                json.dumps(json_safe(arguments.raw), ensure_ascii=False)
            ),
            validation_error.get("details"),
        )
        internal_tool_result = validation_error
    elif handler:
        tool_started = time.perf_counter()
        tool_label = _tool_label(function_name, handlers)
        try:
            if deadline is not None:
                deadline.raise_if_expired()
            async with deadline_guard(deadline):
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
        except DeadlineExceeded:
            _record_tool_metrics(tool_label, tool_started, {"error": "interrupted"})
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

    media_spec = get_media_tool_spec(function_name)
    if media_spec is not None:
        media_spec.log_result(provider_name, internal_tool_result)
    return internal_tool_result
