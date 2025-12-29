from typing import Dict, Optional

from core import config

SYSTEM_PROMPT = config.SYSTEM_PROMPT


def compose_system_prompt(
    tool_context: Optional[Dict[str, object]],
) -> str:
    """Return the base system prompt with any dynamic additions."""
    extra_prompt = ""
    if tool_context:
        dynamic_hint = tool_context.get("user_state_prompt")
        if dynamic_hint:
            extra_prompt = f"{dynamic_hint}"
    return SYSTEM_PROMPT + extra_prompt

