"""主聊天工具集合与处理函数映射，统一由 `tools.specs.TOOL_SPECS` 派生。"""

from .models import AI_TOOL_ARG_MODELS
from .schemas import OPENAI_TOOLS
from .specs import MAIN_TOOL_HANDLERS as AI_TOOL_HANDLERS

__all__ = ["OPENAI_TOOLS", "AI_TOOL_HANDLERS", "AI_TOOL_ARG_MODELS"]
