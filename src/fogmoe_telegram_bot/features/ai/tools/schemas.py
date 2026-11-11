"""OpenAI 函数调用 schema 导出层，统一由 `tools.specs.TOOL_SPECS` 派生。"""

from typing import Any

from .specs import TOOL_SPEC_BY_NAME, agent_tool_schemas

IDLE_RECAP_READ_DIARY_TOOL: dict[str, Any] = TOOL_SPEC_BY_NAME["read_diary_page"].schema()
SUMMARY_SEARCH_PRIOR_CONTEXT_TOOL: dict[str, Any] = TOOL_SPEC_BY_NAME[
    "search_prior_context"
].schema()
OPENAI_TOOLS: list[dict[str, Any]] = agent_tool_schemas("main")

__all__ = [
    "IDLE_RECAP_READ_DIARY_TOOL",
    "OPENAI_TOOLS",
    "SUMMARY_SEARCH_PRIOR_CONTEXT_TOOL",
]
