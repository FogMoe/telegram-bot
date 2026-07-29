"""AI 请求发出前的最终上下文硬预算。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from core.token_estimator import estimate_message_tokens, estimate_tokens


class ContextBudgetExceededError(RuntimeError):
    """固定请求内容本身已经超过上下文硬限制。"""

    def __init__(self, request_tokens: int, token_limit: int) -> None:
        self.request_tokens = request_tokens
        self.token_limit = token_limit
        super().__init__(
            f"AI request requires {request_tokens} tokens, exceeding the hard "
            f"limit of {token_limit}; no content was removed or truncated."
        )


@dataclass(frozen=True)
class ContextBudgetResult:
    messages: list[dict[str, Any]]
    request_tokens: int


def _tool_schema_tokens(
    tools: Sequence[Mapping[str, Any]] | None,
    *,
    model: str | None,
) -> int:
    if not tools:
        return 0
    return estimate_tokens(
        json.dumps(list(tools), ensure_ascii=False, default=str),
        model=model,
    )


def _request_tokens(
    messages: Sequence[Mapping[str, Any]],
    *,
    tools: Sequence[Mapping[str, Any]] | None,
    model: str | None,
    max_output_tokens: int,
    safety_tokens: int,
) -> int:
    return (
        estimate_message_tokens(messages, model=model)
        + _tool_schema_tokens(tools, model=model)
        + max(0, int(max_output_tokens))
        + max(0, int(safety_tokens))
    )


def enforce_messages_context_budget(
    messages: Sequence[Mapping[str, Any]],
    *,
    token_limit: int,
    max_output_tokens: int,
    safety_tokens: int,
    model: str | None = None,
    tools: Sequence[Mapping[str, Any]] | None = None,
) -> ContextBudgetResult:
    """验证最终请求不超过硬限制，不修改任何消息内容。"""
    if token_limit <= 0:
        raise ValueError("token_limit must be positive")

    request_messages = [dict(message) for message in messages]
    request_tokens = _request_tokens(
        request_messages,
        tools=tools,
        model=model,
        max_output_tokens=max_output_tokens,
        safety_tokens=safety_tokens,
    )
    if request_tokens > token_limit:
        raise ContextBudgetExceededError(request_tokens, token_limit)

    return ContextBudgetResult(
        messages=request_messages,
        request_tokens=request_tokens,
    )
