"""Agent 的声明：一个 agent 走哪个任务的 provider 链、用哪些工具、什么系统提示词与循环参数。

工具循环（`tool_runner.run_tool_loop`）只认 `AgentSpec`；按 provider 与模型回退、设置工具请求上下文的
共用运行函数是 `tool_runner.run_agent`。主聊天的回退另有熔断、看图降级与沙箱清理，在 router 里自己调循环。

- `MAIN_AGENT`：主聊天（用户消息、定时任务、空闲跟进的主模型），在这里；
- `SUMMARY_AGENT`：归档摘要，在 `summary.py`；
- `RECAP_AGENT`：空闲跟进前的回顾，在 `idle_followup.py`。

`ToolSpec.agents` 里的名字必须是其中之一，由 `tests/test_tool_specs.py` 检查。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from fogmoe_telegram_bot.core import ai_providers

from .prompts import compose_system_prompt
from .tools.specs import (
    SideEffectKind,
    ToolSpec,
    agent_tool_specs,
    resolve_agent_tool_specs,
)

SystemPromptBuilder = Callable[[Mapping[str, object] | None], str]


@dataclass(frozen=True, slots=True)
class AgentSpec:
    """一个 agent 的声明。

    - `name`：`ToolSpec.agents` 里用的名字；
    - `task`：provider 声明表里的任务（`TaskSpec`），决定 provider 与模型的顺序和需要的能力；
    - `display_name`：日志前缀（主聊天由 router 换成 provider 的显示名）；
    - `system_prompt`：由工具上下文拼出系统提示词；
    - `max_iterations`：工具循环最多几轮，用完后不带工具再要一次最终回复；
    - `context_hard_limit_ratio`、`completion_timeout`、`completion_kwargs`：每次模型调用的参数；
    - `empty_reply_is_failure`：空回复算这次尝试失败，`run_agent` 换下一个模型。
    """

    name: str
    task: str
    display_name: str
    system_prompt: SystemPromptBuilder
    max_iterations: int = 10
    context_hard_limit_ratio: float | None = None
    completion_timeout: int | None = None
    completion_kwargs: Mapping[str, Any] = field(default_factory=dict)
    empty_reply_is_failure: bool = False

    def tools(self, provider: ai_providers.ProviderSpec | None = None) -> tuple[ToolSpec, ...]:
        """这个 agent 在 `provider` 上暴露给模型的工具（见 `resolve_agent_tool_specs`）。"""
        return resolve_agent_tool_specs(self.name, provider=provider)

    @property
    def tools_are_read_only(self) -> bool:
        """工具都是只读的：执行过工具之后的失败也可以换下一个模型从头重跑。"""
        return all(
            spec.side_effect is SideEffectKind.READ_ONLY for spec in agent_tool_specs(self.name)
        )


MAIN_AGENT = AgentSpec(
    name="main",
    task="chat",
    display_name="AI",
    system_prompt=compose_system_prompt,
)


__all__ = ["AgentSpec", "MAIN_AGENT", "SystemPromptBuilder"]
