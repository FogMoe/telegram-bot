from typing import Dict, Optional

from core import config

from ..tool_runner import run_tool_loop
from ..types import AIResponse, VisibleContentHandler


def get_ai_response(
    messages,
    user_id: int,
    tool_context: Optional[Dict[str, object]] = None,
    visible_content_handler: Optional[VisibleContentHandler] = None,
) -> AIResponse:
    """同步版本的 Z.ai（原智谱）响应函数（支持工具调用）"""
    return run_tool_loop(
        "zhipu",
        config.ZHIPU_CHAT_MODEL,
        messages,
        tool_context,
        provider_name="Z.ai",
        skip_tools=("web_search", "web_browser"),
        visible_content_handler=visible_content_handler,
    )

