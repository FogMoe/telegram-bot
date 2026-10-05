"""历史消息发给 provider 之前的协议整理。

差异由 `core.ai_providers` 里每个 provider 声明的 `WireProtocol` 决定。`provider` 参数按名字
取声明的默认协议；调用方已经知道最终协议（例如 Gemini 走 OpenAI-compatible 端点）时直接传 `protocol`。
"""

from typing import Any

from fogmoe_telegram_bot.core import ai_providers
from fogmoe_telegram_bot.core.ai_providers import OPENAI_WIRE, WireProtocol

PROVIDER_SPECIFIC_KEYS = {
    "provider_specific_fields",
}


def _wire_protocol(provider: str, protocol: WireProtocol | None) -> WireProtocol:
    if protocol is not None:
        return protocol
    spec = ai_providers.lookup(provider)
    return spec.wire_protocol if spec is not None else OPENAI_WIRE


def sanitize_tool_call_for_provider(
    tool_call: dict[str, Any],
    provider: str,
    *,
    protocol: WireProtocol | None = None,
) -> dict[str, Any]:
    wire = _wire_protocol(provider, protocol)
    sanitized = dict(tool_call)
    if not wire.keeps_provider_specific_fields:
        for key in PROVIDER_SPECIFIC_KEYS:
            sanitized.pop(key, None)
    if wire.strips_tool_call_ids:
        sanitized.pop("id", None)
    return sanitized


def sanitize_message_for_provider(
    message: dict[str, Any],
    provider: str,
    *,
    protocol: WireProtocol | None = None,
) -> dict[str, Any]:
    wire = _wire_protocol(provider, protocol)
    sanitized = dict(message)
    if not wire.keeps_provider_specific_fields:
        for key in PROVIDER_SPECIFIC_KEYS:
            sanitized.pop(key, None)

    tool_calls = sanitized.get("tool_calls")
    if isinstance(tool_calls, list):
        sanitized["tool_calls"] = [
            sanitize_tool_call_for_provider(tool_call, provider, protocol=wire)
            if isinstance(tool_call, dict)
            else tool_call
            for tool_call in tool_calls
        ]

    if (
        wire.omits_blank_content_on_tool_calls
        and sanitized.get("role") == "assistant"
        and sanitized.get("tool_calls")
        and not str(sanitized.get("content") or "").strip()
    ):
        sanitized.pop("content", None)
    if wire.strips_tool_call_ids and sanitized.get("role") == "tool":
        sanitized.pop("tool_call_id", None)
    return sanitized


def sanitize_messages_for_provider(
    messages: list[dict[str, Any]],
    provider: str,
    *,
    protocol: WireProtocol | None = None,
) -> list[dict[str, Any]]:
    return [
        sanitize_message_for_provider(message, provider, protocol=protocol)
        if isinstance(message, dict)
        else message
        for message in messages
    ]
