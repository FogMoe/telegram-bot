from typing import Any, Dict, List


PROVIDER_SPECIFIC_KEYS = {
    "provider_specific_fields",
}


def sanitize_tool_call_for_provider(
    tool_call: Dict[str, Any],
    provider: str,
) -> Dict[str, Any]:
    sanitized = dict(tool_call)
    if provider != "gemini":
        for key in PROVIDER_SPECIFIC_KEYS:
            sanitized.pop(key, None)
    else:
        sanitized.pop("id", None)
    return sanitized


def sanitize_message_for_provider(
    message: Dict[str, Any],
    provider: str,
) -> Dict[str, Any]:
    sanitized = dict(message)
    if provider != "gemini":
        for key in PROVIDER_SPECIFIC_KEYS:
            sanitized.pop(key, None)

    tool_calls = sanitized.get("tool_calls")
    if isinstance(tool_calls, list):
        sanitized["tool_calls"] = [
            sanitize_tool_call_for_provider(tool_call, provider)
            if isinstance(tool_call, dict)
            else tool_call
            for tool_call in tool_calls
        ]

    if (
        provider == "gemini"
        and sanitized.get("role") == "assistant"
        and sanitized.get("tool_calls")
        and not str(sanitized.get("content") or "").strip()
    ):
        sanitized.pop("content", None)
    if provider == "gemini" and sanitized.get("role") == "tool":
        sanitized.pop("tool_call_id", None)
    return sanitized


def sanitize_messages_for_provider(
    messages: List[Dict[str, Any]],
    provider: str,
) -> List[Dict[str, Any]]:
    return [
        sanitize_message_for_provider(message, provider)
        if isinstance(message, dict)
        else message
        for message in messages
    ]
