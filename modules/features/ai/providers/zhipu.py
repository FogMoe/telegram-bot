from typing import Dict, Optional

from core import config

from ..clients import create_zhipu_client
from ..tool_runner import run_tool_loop
from ..types import AIResponse


def get_ai_response(
    messages,
    user_id: int,
    tool_context: Optional[Dict[str, object]] = None,
) -> AIResponse:
    """同步版本的 Z.ai（原智谱）响应函数（支持工具调用）"""
    client = create_zhipu_client()
    return run_tool_loop(
        client,
        config.ZHIPU_MODEL,
        messages,
        tool_context,
        provider_name="Z.ai",
        skip_tools=("web_search", "web_browser"),
    )

