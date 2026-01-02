from typing import Callable, Dict

from .code_tools import execute_python_code_tool
from .http_tools import fetch_url_tool, google_search_tool
from .memory_tools import (
    fetch_group_context_tool,
    fetch_permanent_summaries_tool,
    get_help_text_tool,
    search_permanent_records_tool,
    user_diary_tool,
)
from .schedule_tools import schedule_ai_message_tool
from .schemas import OPENAI_TOOLS
from .channel_tools import channel_tool
from .user_tools import kindness_gift_tool, update_impression_tool

AI_TOOL_HANDLERS: Dict[str, Callable[..., dict]] = {
    "get_help_text": get_help_text_tool,
    "google_search": google_search_tool,
    "fetch_group_context": fetch_group_context_tool,
    "kindness_gift": kindness_gift_tool,
    "channel_tool": channel_tool,
    "fetch_url": fetch_url_tool,
    "execute_python_code": execute_python_code_tool,
    "update_impression": update_impression_tool,
    "fetch_permanent_summaries": fetch_permanent_summaries_tool,
    "search_permanent_records": search_permanent_records_tool,
    "user_diary": user_diary_tool,
    "schedule_ai_message": schedule_ai_message_tool,
}

__all__ = ["OPENAI_TOOLS", "AI_TOOL_HANDLERS"]
