"""AI 任务的执行与回退。

调用模型的任务（主聊天、摘要、空闲回顾，以及经 `run_ai_task` 的翻译、识图、classifier、advisor）都用
`run_with_fallback` 按同一套规则依次尝试 provider 与模型：

- 顺序：`provider_resolver.get_provider_order_for_task`，每个 provider 先主模型、后备用模型；
- 跳过：未知 provider、缺少任务要求的能力（`TaskSpec.requires`）、没有配置模型、处于熔断冷却中；
- 不换：上下文超限（与 provider 无关）、整轮截止时间到期，以及执行过工具或发出过可见内容之后的失败
  （`PartialAIResponseError`，工具都是只读的任务除外）——这些原样上抛；
- 安全拦截：声明了 `translates_safety_blocks` 的 provider 把「被安全策略拦截」的错误转成 `SafetyBlockError`。
  还有备用模型就换模型；模型用完时，声明了 `safety_blocks_fall_through` 的换下一个 provider（不计熔断），
  其余上抛；
- 其他失败换下一个模型或 provider。传了 `circuit` 的任务在一个 provider 的模型都失败时记一次熔断失败；
- 传了 `deadline` 的任务（主聊天）：看每个 provider（包括要跳过的）、试每个模型之前都检查，到期就抛
  `TurnDeadlineError`（`phase="fallback"`），不再尝试，也不记熔断失败。

每次尝试做什么由调用方的 `attempt` 决定（一次模型调用，或一次工具循环），工具请求上下文也由 `attempt`
用 `tools.context.tool_request_scope` 设置：这个模块在 advisor 工具的调用链上，不能 import 工具包。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fogmoe_telegram_bot.core import ai_providers
from fogmoe_telegram_bot.core.deadline import Deadline

from .errors import SafetyBlockError, find_context_budget_error
from .litellm_client import create_chat_completion
from .provider_resolver import (
    get_models_for_task,
    get_provider_order_for_task,
    missing_capability_for_task,
)
from .types import PartialAIResponseError, TurnDeadlineError


@dataclass(frozen=True, slots=True)
class Candidate:
    """一次尝试：一个已声明的 provider，与它在这个任务上配置的一个模型。"""

    spec: ai_providers.ProviderSpec
    model: str

    @property
    def provider(self) -> str:
        return self.spec.name


class AllProvidersFailedError(RuntimeError):
    """所有 provider 与模型都失败或被跳过。`last_error` 是最后一次失败，全部被跳过时可能是 None。"""

    def __init__(self, task: str, last_error: Exception | None) -> None:
        super().__init__(f"All providers failed for AI task: {task}")
        self.task = task
        self.last_error = last_error


class ProviderCircuit:
    """按 provider 计的熔断：`window_seconds` 内连续失败 `failure_threshold` 次，冷却 `cooldown_seconds`。

    状态在进程内存里，重启清零。
    """

    def __init__(
        self,
        *,
        failure_threshold: int,
        window_seconds: float,
        cooldown_seconds: float,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.window_seconds = window_seconds
        self.cooldown_seconds = cooldown_seconds
        self._failures: dict[str, list[float]] = {}
        self._open_until: dict[str, float] = {}

    def is_open(self, provider: str, now: float | None = None) -> bool:
        current_time = time.monotonic() if now is None else now
        open_until = self._open_until.get(provider)
        if not open_until:
            return False
        if current_time < open_until:
            return True

        self._open_until.pop(provider, None)
        self._failures.pop(provider, None)
        return False

    def record_success(self, provider: str) -> None:
        self._failures.pop(provider, None)
        self._open_until.pop(provider, None)

    def record_failure(self, provider: str, now: float | None = None) -> None:
        current_time = time.monotonic() if now is None else now
        cutoff = current_time - self.window_seconds
        recent_failures = [
            failure_time
            for failure_time in self._failures.get(provider, [])
            if failure_time >= cutoff
        ]
        recent_failures.append(current_time)
        self._failures[provider] = recent_failures

        if len(recent_failures) >= self.failure_threshold:
            self._open_until[provider] = current_time + self.cooldown_seconds
            self._failures.pop(provider, None)
            logging.warning(
                "%s 熔断 %s 秒：%s 秒内连续失败 %s 次",
                provider,
                self.cooldown_seconds,
                self.window_seconds,
                self.failure_threshold,
            )

    def failure_count(self, provider: str) -> int:
        """还没有触发熔断的连续失败次数。"""
        return len(self._failures.get(provider, []))

    def reset(self) -> None:
        self._failures.clear()
        self._open_until.clear()


def _raise_if_deadline_expired(task: str, deadline: Deadline | None) -> None:
    if deadline is not None and deadline.expired:
        logging.warning(
            "AI task %s stopped falling back: turn deadline reached (%s)",
            task,
            deadline.expiry_reason,
        )
        raise TurnDeadlineError(deadline.expiry_reason, "fallback", [])


def _as_safety_block(spec: ai_providers.ProviderSpec, exc: Exception) -> SafetyBlockError | None:
    if isinstance(exc, SafetyBlockError):
        return exc
    error_text = str(exc)
    if spec.translates_safety_blocks and "SAFETY" in error_text and "blocked" in error_text:
        safety_error = SafetyBlockError(error_text)
        safety_error.__cause__ = exc
        return safety_error
    return None


async def run_with_fallback[T](
    task: str,
    attempt: Callable[[Candidate], Awaitable[T]],
    *,
    tools_are_read_only: bool = False,
    circuit: ProviderCircuit | None = None,
    deadline: Deadline | None = None,
) -> T:
    """按任务的 provider 与模型顺序调用 `attempt`，返回第一次成功的结果；规则见模块说明。

    `tools_are_read_only`：任务的工具都是只读的（摘要、回顾），执行过工具之后的失败也可以换下一个模型
    从头重跑。全部失败或被跳过时抛 `AllProvidersFailedError`。
    """
    last_error: Exception | None = None
    for name in get_provider_order_for_task(task):
        _raise_if_deadline_expired(task, deadline)
        spec = ai_providers.lookup(name)
        if spec is None:
            logging.warning("AI task %s skipped unknown provider %s", task, name)
            last_error = RuntimeError(f"Unsupported AI provider: {name}")
            continue
        missing_capability = missing_capability_for_task(spec.name, task)
        if missing_capability:
            logging.warning(
                "AI task %s skipped provider %s: no %s support",
                task,
                spec.name,
                missing_capability,
            )
            continue
        if circuit is not None and circuit.is_open(spec.name):
            logging.warning("AI task %s skipped provider %s: circuit open", task, spec.name)
            continue
        models = get_models_for_task(spec.name, task)
        if not models:
            logging.warning("AI task %s skipped provider %s: no model configured", task, spec.name)
            continue

        for index, model in enumerate(models):
            _raise_if_deadline_expired(task, deadline)
            error: Exception
            try:
                result = await attempt(Candidate(spec, model))
            except PartialAIResponseError as exc:
                if (
                    not tools_are_read_only
                    or isinstance(exc, TurnDeadlineError)
                    or find_context_budget_error(exc) is not None
                ):
                    raise
                error = exc
            except Exception as exc:
                if find_context_budget_error(exc) is not None:
                    raise
                error = _as_safety_block(spec, exc) or exc
                last_model = index == len(models) - 1
                if (
                    isinstance(error, SafetyBlockError)
                    and last_model
                    and not spec.safety_blocks_fall_through
                ):
                    raise error
            else:
                if circuit is not None:
                    circuit.record_success(spec.name)
                return result
            logging.warning(
                "AI task %s failed via provider=%s model=%s: %s",
                task,
                spec.name,
                model,
                error,
            )
            last_error = error

        if circuit is not None and not isinstance(last_error, SafetyBlockError):
            circuit.record_failure(spec.name)

    raise AllProvidersFailedError(task, last_error) from last_error


async def run_ai_task(
    task: str,
    messages: list[dict[str, Any]],
    **kwargs: Any,
) -> Any:
    """一次模型调用（翻译、识图、classifier、advisor），按任务的 provider 与模型回退；`kwargs` 原样传给模型调用。"""

    async def attempt(candidate: Candidate) -> Any:
        return await create_chat_completion(candidate.provider, candidate.model, messages, **kwargs)

    return await run_with_fallback(task, attempt)
