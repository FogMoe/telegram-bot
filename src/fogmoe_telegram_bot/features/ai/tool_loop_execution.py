"""工具循环里的一次工具执行：解析与校验参数、调用处理函数、记录日志与指标。

处理函数按 `ToolSpec.execution` 调用（async、同步走有界线程适配器、内联），见 `tools/dispatch.py`。工具出错不会让这一轮失败：
参数校验失败、工具未开放或未知、处理函数抛异常都变成带 `error` 的结果交回模型。只有整轮截止时间到期
（`DeadlineExceeded`）向上抛，由工具循环给这一轮补齐结果。
"""

import json
import logging
import time
from collections.abc import Mapping
from typing import Any, NamedTuple

from pydantic import ValidationError

from fogmoe_telegram_bot.core import blocking, metrics
from fogmoe_telegram_bot.core.deadline import Deadline, DeadlineExceeded
from fogmoe_telegram_bot.core.redaction import describe_exception, log_exception, redact_text

from .tool_loop_deadline import deadline_guard
from .tool_loop_messages import json_safe
from .tools import ToolExecution, ToolSpec, get_media_tool_spec, get_tool_spec
from .types import ToolCallStatus

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
    spec = get_tool_spec(function_name)
    if spec is None:
        if isinstance(raw_args, dict):
            return raw_args, None
        return {}, None

    try:
        validated = spec.arg_model.model_validate(raw_args)
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


async def _call_tool(spec: ToolSpec, arguments: dict[str, Any]) -> Any:
    """async 工具直接 await；同步工具走有界的线程适配器（并发有上限，保留 contextvars）；
    声明为内联的纯内存工具直接调用。见 `tools/dispatch.py`。"""
    if spec.execution is ToolExecution.ASYNC:
        return await spec.handler(**arguments)
    if spec.execution is ToolExecution.INLINE:
        return spec.handler(**arguments)
    return await blocking.tools().run(spec.handler, **arguments)


class ToolOutcome(NamedTuple):
    """一次工具调用的内部结果，以及处理函数运行过（`COMPLETED`）还是调用被拒绝（`REJECTED`）。"""

    result: Any
    status: ToolCallStatus


def _record_tool_metrics(label: str, started: float, result: Any) -> None:
    metrics.counter("tool.calls", tool=label).inc()
    metrics.histogram("tool.seconds", tool=label).observe(time.perf_counter() - started)
    if isinstance(result, dict) and result.get("error"):
        metrics.counter("tool.failures", tool=label).inc()


async def execute_tool_call(
    function_name: str,
    arguments: ToolArguments,
    *,
    tools: Mapping[str, ToolSpec],
    provider_name: str,
    deadline: Deadline | None = None,
) -> ToolOutcome:
    """执行一个工具调用，返回工具的内部结果。截止时间到期时记一次 interrupted 指标后向上抛。

    `tools` 是这个 agent 在这个 provider 上开放的工具；不在其中的调用（模型编造的名字，或注册过、
    但对这次请求隐藏的工具）返回「未开放」错误，不运行处理函数。
    """
    function_args = arguments.validated
    validation_error = arguments.validation_error
    spec = tools.get(function_name)
    status = ToolCallStatus.REJECTED
    if not function_name:
        logging.warning("%s 返回的工具调用缺少函数名: args=%s", provider_name, arguments.raw)
        internal_tool_result: Any = {"error": "Tool call is missing function name"}
    elif spec is None:
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
    else:
        status = ToolCallStatus.COMPLETED
        tool_started = time.perf_counter()
        # 指标标签只用开放的工具名，模型编造的名字在上面就被拒绝了。
        tool_label = function_name
        try:
            if deadline is not None:
                deadline.raise_if_expired()
            async with deadline_guard(deadline):
                internal_tool_result = await _call_tool(spec, function_args)
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

    media_spec = get_media_tool_spec(function_name)
    if media_spec is not None:
        media_spec.log_result(provider_name, internal_tool_result)
    return ToolOutcome(internal_tool_result, status)
