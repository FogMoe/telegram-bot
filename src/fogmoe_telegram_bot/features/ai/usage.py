"""AI 模型调用与外部计费工具的开销计量。

所有经 `litellm_client.create_chat_completion` 的模型调用统一在这里提取输入 token、缓存命中输入
token、输出 token 与美元成本，同时更新：

1. 进程级指标（`core.metrics`），标签严格限制在有限集合 `{provider, task, entry}`，绝不放 `user_id`；
2. 当前上下文绑定的单轮累加器（`UsageAccumulator`），供对话轮次、定时任务与空闲跟进在收尾日志里写出
   本轮总调用次数、token、美元成本与工具调用数，方便与本轮扣除的用户硬币对照校准。
"""

from __future__ import annotations

import contextvars
import json
import logging
import math
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

import litellm

from fogmoe_telegram_bot.core import ai_providers, config, metrics

logger = logging.getLogger(__name__)

VALID_ENTRIES = frozenset({"user", "schedule", "idle_followup", "command", "background"})
DEFAULT_ENTRY = "background"
UNKNOWN_TASK = "unknown"

# 按次或按时长向外部服务付费的非 LLM 工具；第 4 步接入 ToolSpec 后与声明保持一致。
DEFAULT_BILLED_TOOL_CATEGORIES: Mapping[str, str] = MappingProxyType(
    {
        "google_search": "search",
        "execute_python_code": "code_exec",
        "linux_sandbox": "sandbox",
        "generate_image": "image_gen",
        "generate_voice": "voice_gen",
    }
)

_current_entry: contextvars.ContextVar[str] = contextvars.ContextVar(
    "ai_usage_entry",
    default=DEFAULT_ENTRY,
)
_current_task: contextvars.ContextVar[str] = contextvars.ContextVar(
    "ai_usage_task",
    default=UNKNOWN_TASK,
)
_current_accumulator: contextvars.ContextVar[UsageAccumulator | None] = (
    contextvars.ContextVar("ai_usage_accumulator", default=None)
)


@dataclass(frozen=True, slots=True)
class ModelPriceRate:
    """每百万 token 的美元单价。"""

    input_per_1m: float
    output_per_1m: float
    cached_input_per_1m: float | None = None

    def cost_usd(
        self,
        *,
        prompt_tokens: int,
        cached_tokens: int,
        completion_tokens: int,
    ) -> float:
        clamped_cached = min(max(cached_tokens, 0), max(prompt_tokens, 0))
        uncached_input = max(prompt_tokens - clamped_cached, 0)
        cached_rate = (
            self.input_per_1m
            if self.cached_input_per_1m is None
            else self.cached_input_per_1m
        )
        return (
            uncached_input * self.input_per_1m
            + clamped_cached * cached_rate
            + max(completion_tokens, 0) * self.output_per_1m
        ) / 1_000_000.0


@dataclass(frozen=True, slots=True)
class CallUsage:
    provider: str
    model: str
    task: str
    entry: str
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0


@dataclass(frozen=True, slots=True)
class TurnUsageSummary:
    ai_calls: int = 0
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    tool_calls: int = 0
    billed_tool_calls: int = 0

    def summary(self) -> str:
        return (
            f"ai_calls={self.ai_calls} "
            f"in_tokens={self.prompt_tokens} "
            f"cached_tokens={self.cached_tokens} "
            f"out_tokens={self.completion_tokens} "
            f"cost_usd={self.cost_usd:.6f} "
            f"tool_calls={self.tool_calls} "
            f"billed_tool_calls={self.billed_tool_calls}"
        )


class UsageAccumulator:
    """单轮（或单次后台任务）内的线程安全用量累加器。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ai_calls = 0
        self._prompt_tokens = 0
        self._cached_tokens = 0
        self._completion_tokens = 0
        self._total_tokens = 0
        self._cost_usd = 0.0
        self._tool_calls = 0
        self._billed_tool_calls = 0

    def record_ai_call(self, usage: CallUsage) -> None:
        with self._lock:
            self._ai_calls += 1
            self._prompt_tokens += usage.prompt_tokens
            self._cached_tokens += usage.cached_tokens
            self._completion_tokens += usage.completion_tokens
            self._total_tokens += usage.total_tokens
            self._cost_usd += usage.cost_usd

    def record_tool_call(self, *, billed: bool = False) -> None:
        with self._lock:
            self._tool_calls += 1
            if billed:
                self._billed_tool_calls += 1

    def snapshot(self) -> TurnUsageSummary:
        with self._lock:
            return TurnUsageSummary(
                ai_calls=self._ai_calls,
                prompt_tokens=self._prompt_tokens,
                cached_tokens=self._cached_tokens,
                completion_tokens=self._completion_tokens,
                total_tokens=self._total_tokens,
                cost_usd=self._cost_usd,
                tool_calls=self._tool_calls,
                billed_tool_calls=self._billed_tool_calls,
            )


def _normalize_entry(entry: str) -> str:
    cleaned = entry.strip().lower()
    return cleaned if cleaned in VALID_ENTRIES else DEFAULT_ENTRY


def _normalize_task(task: str) -> str:
    cleaned = task.strip().lower()
    return cleaned if cleaned in ai_providers.TASK_SPECS else UNKNOWN_TASK


def current_usage_entry() -> str:
    return _current_entry.get()


def current_usage_task() -> str:
    return _current_task.get()


def current_usage_accumulator() -> UsageAccumulator | None:
    return _current_accumulator.get()


@contextmanager
def ai_usage_scope(
    *,
    entry: str | None = None,
    task: str | None = None,
    accumulator: UsageAccumulator | None = None,
    clear_accumulator: bool = False,
) -> Iterator[None]:
    """设置当前异步调用链上的 `entry`、`task` 标签与可选的 `UsageAccumulator`。"""
    tokens: list[tuple[contextvars.ContextVar[Any], contextvars.Token[Any]]] = []
    if entry is not None:
        tokens.append((_current_entry, _current_entry.set(_normalize_entry(entry))))
    if task is not None:
        tokens.append((_current_task, _current_task.set(_normalize_task(task))))
    if clear_accumulator:
        tokens.append((_current_accumulator, _current_accumulator.set(accumulator)))
    elif accumulator is not None:
        tokens.append((_current_accumulator, _current_accumulator.set(accumulator)))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


_pricing_cache_lock = threading.Lock()
_pricing_cache_raw: str | None = None
_pricing_cache_map: Mapping[str, ModelPriceRate] = MappingProxyType({})


def parse_model_pricing(raw: str | None) -> Mapping[str, ModelPriceRate]:
    """解析 `AI_MODEL_PRICING` JSON 配置：键是模型名或 `provider/model`（大小写不敏感）。"""
    text = (raw or "").strip()
    if not text:
        return MappingProxyType({})
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Ignoring invalid AI_MODEL_PRICING JSON configuration")
        return MappingProxyType({})
    if not isinstance(parsed, Mapping):
        logger.warning("Ignoring non-object AI_MODEL_PRICING configuration")
        return MappingProxyType({})

    rates: dict[str, ModelPriceRate] = {}
    for raw_key, spec in parsed.items():
        key = str(raw_key).strip().lower()
        if not key or not isinstance(spec, Mapping):
            continue
        try:
            input_rate = float(spec.get("input", 0.0))
            output_rate = float(spec.get("output", 0.0))
            cached_raw = spec.get("cached_input")
            cached_rate = float(cached_raw) if cached_raw is not None else None
        except (TypeError, ValueError):
            logger.warning("Ignoring invalid AI_MODEL_PRICING entry for %s", raw_key)
            continue
        if input_rate < 0 or output_rate < 0 or (cached_rate is not None and cached_rate < 0):
            continue
        rates[key] = ModelPriceRate(
            input_per_1m=input_rate,
            output_per_1m=output_rate,
            cached_input_per_1m=cached_rate,
        )
    return MappingProxyType(rates)


def _configured_pricing(settings: Any = None) -> Mapping[str, ModelPriceRate]:
    global _pricing_cache_raw, _pricing_cache_map
    source = config if settings is None else settings
    raw = str(getattr(source, "AI_MODEL_PRICING", "") or "")
    if settings is not None:
        return parse_model_pricing(raw)
    with _pricing_cache_lock:
        if raw != _pricing_cache_raw:
            _pricing_cache_raw = raw
            _pricing_cache_map = parse_model_pricing(raw)
        return _pricing_cache_map


def _read_field(obj: Any, name: str) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def _non_negative_int(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(parsed, 0)


def extract_token_counts(response: Any) -> tuple[int, int, int, int]:
    """从 LiteLLM 响应对象或字典中提取 `(prompt_tokens, cached_tokens, completion_tokens, total_tokens)`。"""
    usage = _read_field(response, "usage")
    if usage is None:
        return 0, 0, 0, 0

    prompt_tokens = _non_negative_int(_read_field(usage, "prompt_tokens"))
    completion_tokens = _non_negative_int(_read_field(usage, "completion_tokens"))
    raw_total = _non_negative_int(_read_field(usage, "total_tokens"))
    total_tokens = raw_total if raw_total > 0 else prompt_tokens + completion_tokens

    details = _read_field(usage, "prompt_tokens_details")
    cached_tokens = _non_negative_int(_read_field(details, "cached_tokens"))
    if cached_tokens == 0:
        cached_tokens = _non_negative_int(_read_field(usage, "cache_read_input_tokens"))
    cached_tokens = min(cached_tokens, prompt_tokens)
    return prompt_tokens, cached_tokens, completion_tokens, total_tokens


def estimate_call_cost_usd(
    *,
    provider: str,
    model: str,
    litellm_model: str,
    response: Any,
    prompt_tokens: int,
    cached_tokens: int,
    completion_tokens: int,
    settings: Any = None,
) -> float:
    """估算单次模型调用的美元成本：自定义价格表优先，其次 LiteLLM，未知模型返回 0。"""
    pricing = _configured_pricing(settings)
    if pricing:
        for candidate_key in (
            f"{provider}/{model}".lower(),
            litellm_model.lower(),
            model.lower(),
        ):
            rate = pricing.get(candidate_key)
            if rate is not None:
                return rate.cost_usd(
                    prompt_tokens=prompt_tokens,
                    cached_tokens=cached_tokens,
                    completion_tokens=completion_tokens,
                )

    hidden_params = getattr(response, "_hidden_params", None)
    if isinstance(hidden_params, Mapping):
        raw_cost = hidden_params.get("response_cost")
        if isinstance(raw_cost, (int, float)) and math.isfinite(raw_cost) and raw_cost >= 0:
            return float(raw_cost)

    if prompt_tokens > 0 or completion_tokens > 0:
        try:
            calculated = litellm.completion_cost(completion_response=response)
        except Exception:
            calculated = None
        if (
            isinstance(calculated, (int, float))
            and math.isfinite(calculated)
            and calculated >= 0
        ):
            return float(calculated)

    return 0.0


def record_completion_usage(
    provider: str,
    model: str,
    *,
    litellm_model: str,
    response: Any,
    settings: Any = None,
) -> CallUsage:
    """提取一次成功模型调用的用量，写入进程指标与当前单轮累加器。"""
    provider_label = (provider or "unknown").strip().lower() or "unknown"
    task_label = current_usage_task()
    entry_label = current_usage_entry()
    prompt_tokens, cached_tokens, completion_tokens, total_tokens = extract_token_counts(
        response
    )
    cost_usd = estimate_call_cost_usd(
        provider=provider_label,
        model=model,
        litellm_model=litellm_model,
        response=response,
        prompt_tokens=prompt_tokens,
        cached_tokens=cached_tokens,
        completion_tokens=completion_tokens,
        settings=settings,
    )
    call_usage = CallUsage(
        provider=provider_label,
        model=model,
        task=task_label,
        entry=entry_label,
        prompt_tokens=prompt_tokens,
        cached_tokens=cached_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        cost_usd=cost_usd,
    )

    labels = {
        "provider": provider_label,
        "task": task_label,
        "entry": entry_label,
    }
    metrics.counter("ai.calls", **labels).inc()
    if prompt_tokens > 0:
        metrics.counter("ai.input_tokens", **labels).inc(prompt_tokens)
    if cached_tokens > 0:
        metrics.counter("ai.cached_input_tokens", **labels).inc(cached_tokens)
    if completion_tokens > 0:
        metrics.counter("ai.output_tokens", **labels).inc(completion_tokens)
    cost_microusd = round(cost_usd * 1_000_000)
    if cost_microusd > 0:
        metrics.counter("ai.cost_microusd", **labels).inc(cost_microusd)

    accumulator = current_usage_accumulator()
    if accumulator is not None:
        accumulator.record_ai_call(call_usage)
    return call_usage


def record_tool_execution_usage(
    tool_label: str,
    *,
    billing_category: str | None = None,
    failed: bool = False,
) -> None:
    """记录一次已实际发起的工具执行，区分外部付费工具与免费内部工具。"""
    category = (
        DEFAULT_BILLED_TOOL_CATEGORIES.get(tool_label)
        if billing_category is None
        else billing_category
    )
    billed = bool(category) and not failed
    if billed and category is not None:
        metrics.counter("tool.billed_calls", tool=tool_label, category=category).inc()
    accumulator = current_usage_accumulator()
    if accumulator is not None:
        accumulator.record_tool_call(billed=billed)


__all__ = [
    "CallUsage",
    "DEFAULT_BILLED_TOOL_CATEGORIES",
    "ModelPriceRate",
    "TurnUsageSummary",
    "UsageAccumulator",
    "VALID_ENTRIES",
    "ai_usage_scope",
    "current_usage_accumulator",
    "current_usage_entry",
    "current_usage_task",
    "estimate_call_cost_usd",
    "extract_token_counts",
    "parse_model_pricing",
    "record_completion_usage",
    "record_tool_execution_usage",
]
