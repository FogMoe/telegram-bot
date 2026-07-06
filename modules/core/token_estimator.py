from __future__ import annotations

import json
import logging
import math
from typing import Any, Iterable, Mapping, Tuple

import litellm

DEFAULT_GUARD_RATIO = 1.15
DEFAULT_MESSAGE_OVERHEAD = 4.0

EN_WEIGHT = 1.0 / 3.0
ZH_WEIGHT = 1.1
OTHER_WEIGHT = 1.8


def estimate_tokens(
    text: str,
    *,
    guard_ratio: float | None = DEFAULT_GUARD_RATIO,
    model: str | None = None,
) -> int:
    """Estimate tokens for a text string using a conservative heuristic."""
    litellm_count = _count_litellm_tokens(model=model, text=text) if model else None
    if litellm_count is not None:
        return _apply_guard_and_round(float(litellm_count), guard_ratio=guard_ratio)

    estimate = estimate_tokens_raw(text)
    return _apply_guard_and_round(estimate, guard_ratio=guard_ratio)


def estimate_message_tokens(
    messages: Iterable[Mapping[str, Any]],
    *,
    guard_ratio: float | None = DEFAULT_GUARD_RATIO,
    per_message_overhead: float = DEFAULT_MESSAGE_OVERHEAD,
    include_tool_calls: bool = True,
    model: str | None = None,
) -> int:
    """Estimate tokens for a list of chat messages."""
    message_list = list(messages)
    litellm_count = (
        _count_litellm_tokens(
            model=model,
            messages=_prepare_messages_for_litellm(
                message_list,
                include_tool_calls=include_tool_calls,
            ),
        )
        if model
        else None
    )
    if litellm_count is not None:
        return _apply_guard_and_round(float(litellm_count), guard_ratio=guard_ratio)

    total = estimate_message_tokens_raw(
        message_list,
        per_message_overhead=per_message_overhead,
        include_tool_calls=include_tool_calls,
    )

    return _apply_guard_and_round(total, guard_ratio=guard_ratio)


def estimate_message_tokens_raw(
    messages: Iterable[Mapping[str, Any]],
    *,
    per_message_overhead: float = DEFAULT_MESSAGE_OVERHEAD,
    include_tool_calls: bool = True,
) -> float:
    """Estimate tokens for a list of chat messages without guard or rounding."""
    total = 0.0
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        total += per_message_overhead
        content = message.get("content")
        if content:
            total += estimate_tokens_raw(str(content))

        if include_tool_calls:
            tool_calls = message.get("tool_calls")
            if tool_calls:
                try:
                    tool_payload = json.dumps(tool_calls, ensure_ascii=False)
                except TypeError:
                    tool_payload = str(tool_calls)
                total += estimate_tokens_raw(tool_payload)
    return total


def estimate_conversation_tokens(
    messages: Iterable[Mapping[str, Any]],
    *,
    system_prompt: str | None = None,
    system_prompt_extra: str | None = None,
    guard_ratio: float | None = DEFAULT_GUARD_RATIO,
    per_message_overhead: float = DEFAULT_MESSAGE_OVERHEAD,
    include_tool_calls: bool = True,
    model: str | None = None,
) -> int:
    """Estimate tokens for a conversation including system prompt contributions."""
    message_list = list(messages)
    litellm_messages = _prepare_messages_for_litellm(
        message_list,
        system_prompt=system_prompt,
        system_prompt_extra=system_prompt_extra,
        include_tool_calls=include_tool_calls,
    )
    litellm_count = (
        _count_litellm_tokens(model=model, messages=litellm_messages)
        if model
        else None
    )
    if litellm_count is not None:
        return _apply_guard_and_round(float(litellm_count), guard_ratio=guard_ratio)

    total = estimate_message_tokens_raw(
        message_list,
        per_message_overhead=per_message_overhead,
        include_tool_calls=include_tool_calls,
    )
    if system_prompt:
        total += estimate_tokens_raw(system_prompt)
    if system_prompt_extra:
        total += estimate_tokens_raw(system_prompt_extra)
    return _apply_guard_and_round(total, guard_ratio=guard_ratio)


def estimate_tokens_raw(text: str) -> float:
    if not text:
        return 0.0
    en_chars, zh_chars, other_chars = _count_char_categories(text)
    return (en_chars * EN_WEIGHT) + (zh_chars * ZH_WEIGHT) + (other_chars * OTHER_WEIGHT)


def _apply_guard_and_round(
    token_count: float,
    *,
    guard_ratio: float | None,
) -> int:
    if guard_ratio:
        token_count *= guard_ratio
    return int(math.ceil(token_count)) if token_count > 0 else 0


def _prepare_messages_for_litellm(
    messages: Iterable[Mapping[str, Any]],
    *,
    system_prompt: str | None = None,
    system_prompt_extra: str | None = None,
    include_tool_calls: bool = True,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    system_content = f"{system_prompt or ''}{system_prompt_extra or ''}"
    if system_content:
        result.append({"role": "system", "content": system_content})

    for message in messages:
        if not isinstance(message, Mapping):
            continue
        prepared = dict(message)
        if not include_tool_calls:
            prepared.pop("tool_calls", None)
        result.append(prepared)
    return result


def _count_litellm_tokens(
    *,
    model: str | None,
    text: str | None = None,
    messages: list[dict[str, Any]] | None = None,
) -> int | None:
    try:
        count = litellm.token_counter(
            model=model or "",
            text=text,
            messages=messages,
            use_default_image_token_count=True,
        )
    except Exception as exc:
        logging.debug(
            "LiteLLM token counting failed; falling back to heuristic: %s",
            exc,
        )
        return None

    try:
        return int(count)
    except (TypeError, ValueError):
        logging.debug(
            "LiteLLM token counting returned non-integer value %r; falling back to heuristic",
            count,
        )
        return None


def _count_char_categories(text: str) -> Tuple[int, int, int]:
    en_chars = 0
    zh_chars = 0
    other_chars = 0

    for ch in text:
        codepoint = ord(ch)
        if codepoint <= 0x7F:
            en_chars += 1
        elif _is_cjk(codepoint):
            zh_chars += 1
        else:
            other_chars += 1

    return en_chars, zh_chars, other_chars


def _is_cjk(codepoint: int) -> bool:
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x2A6DF
        or 0x2A700 <= codepoint <= 0x2B73F
        or 0x2B740 <= codepoint <= 0x2B81F
        or 0x2B820 <= codepoint <= 0x2CEAF
        or 0x2F800 <= codepoint <= 0x2FA1F
    )
