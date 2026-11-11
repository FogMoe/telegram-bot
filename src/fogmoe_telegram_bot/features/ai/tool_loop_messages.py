"""工具循环的消息转换：把模型返回的 assistant 消息与 tool_calls 整理成可以回填、可以记录的普通 dict。

provider 返回的对象形态不一（LiteLLM 的 pydantic 对象、dict、带 `provider_specific_fields` 的变体），
这里统一成 JSON 安全的结构；`resolve_assistant_message` 还负责声明了 `merges_split_tool_call_choices`
的 provider 的协议兼容，见 docs/ai-provider-architecture.md。
"""

import base64
import json
import logging
from typing import Any

from fogmoe_telegram_bot.core import ai_providers


def json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    for attr in ("model_dump", "dict"):
        if hasattr(value, attr):
            dump_func = getattr(value, attr)
            for kwargs in ({"mode": "json"}, {}, {"by_alias": True}):
                try:
                    dumped = dump_func(**kwargs)
                except TypeError:
                    continue
                except Exception:
                    break
                return json_safe(dumped)
    return str(value)


def tool_result_json(result: Any) -> str:
    """工具结果的 JSON 文本：回填给模型和写进历史用同一种序列化，不能直接序列化的值转成字符串。"""
    try:
        return json.dumps(result, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(result)


def _drop_none_items(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None}


def _tool_call_to_plain(tool_call: Any) -> dict[str, Any]:
    """Normalize a tool call object into a plain JSON-serializable dict."""
    if isinstance(tool_call, dict):
        plain_call = json_safe(dict(tool_call))
        function_payload = plain_call.get("function")
        if isinstance(function_payload, dict):
            plain_function = dict(function_payload)
            arguments = plain_function.get("arguments")
            if isinstance(arguments, (dict, list)):
                plain_function["arguments"] = json.dumps(arguments, ensure_ascii=False)
            elif arguments is None:
                plain_function["arguments"] = "{}"
            plain_call["function"] = plain_function
        return _drop_none_items(plain_call)
    plain_call: dict[str, Any] | None = None

    for attr in ("model_dump", "dict"):
        if hasattr(tool_call, attr):
            try:
                plain_call = getattr(tool_call, attr)(mode="json")
            except TypeError:
                try:
                    plain_call = getattr(tool_call, attr)()
                except TypeError:
                    plain_call = getattr(tool_call, attr)(by_alias=True)
            except Exception:
                plain_call = None
            if isinstance(plain_call, dict):
                plain_call = json_safe(plain_call)
                break

    if not isinstance(plain_call, dict):
        function = getattr(tool_call, "function", None)
        arguments = getattr(function, "arguments", None) if function else None
        if isinstance(arguments, (dict, list)):
            arguments_str = json.dumps(arguments, ensure_ascii=False)
        else:
            arguments_str = arguments if arguments is not None else "{}"

        plain_call = {
            "id": getattr(tool_call, "id", None),
            "type": getattr(tool_call, "type", "function"),
            "function": {
                "name": getattr(function, "name", None) if function else None,
                "arguments": arguments_str,
            },
        }
        provider_specific_fields = getattr(
            tool_call,
            "provider_specific_fields",
            None,
        )
        if provider_specific_fields:
            plain_call["provider_specific_fields"] = json_safe(provider_specific_fields)
        return _drop_none_items(plain_call)

    function_payload = plain_call.get("function")
    if not isinstance(function_payload, dict):
        for attr in ("model_dump", "dict"):
            if hasattr(function_payload, attr):
                try:
                    function_payload = getattr(function_payload, attr)()
                except TypeError:
                    function_payload = getattr(function_payload, attr)(by_alias=True)
                except Exception:
                    function_payload = None
                if isinstance(function_payload, dict):
                    plain_call["function"] = function_payload
                break

    if isinstance(function_payload, dict):
        plain_function = dict(function_payload)
        arguments = plain_function.get("arguments")
        if isinstance(arguments, (dict, list)):
            plain_function["arguments"] = json.dumps(arguments, ensure_ascii=False)
        elif arguments is None:
            plain_function["arguments"] = "{}"
        plain_call["function"] = plain_function

    return _drop_none_items(plain_call)


def _message_to_plain_dict(message: Any) -> dict[str, Any]:
    if isinstance(message, dict):
        return _drop_none_items(json_safe(dict(message)))

    for attr in ("model_dump", "dict"):
        if hasattr(message, attr):
            dump_func = getattr(message, attr)
            for kwargs in ({"mode": "json"}, {}, {"by_alias": True}):
                try:
                    dumped = dump_func(**kwargs)
                except TypeError:
                    continue
                except Exception:
                    break
                if isinstance(dumped, dict):
                    return _drop_none_items(json_safe(dumped))

    result: dict[str, Any] = {}
    for key in (
        "role",
        "content",
        "tool_calls",
        "function_call",
        "provider_specific_fields",
        "reasoning_content",
    ):
        value = getattr(message, key, None)
        if value is not None:
            result[key] = json_safe(value)
    return _drop_none_items(result)


def assistant_message_to_plain(
    assistant_message: Any,
    *,
    content: str,
    tool_calls: list[dict[str, Any]],
) -> dict[str, Any]:
    message = _message_to_plain_dict(assistant_message)
    message["role"] = "assistant"
    message["content"] = content
    if tool_calls:
        message["tool_calls"] = tool_calls
    else:
        message.pop("tool_calls", None)
    return message


def normalise_tool_calls(
    tool_calls: list[Any] | None,
    *,
    round_index: int = 1,
) -> list[dict[str, Any]]:
    if not tool_calls:
        return []
    normalised: list[dict[str, Any]] = []
    for call_index, call in enumerate(tool_calls, start=1):
        plain = _tool_call_to_plain(call)
        raw_id = plain.get("id")
        call_id = str(raw_id).strip() if raw_id is not None else ""
        if not call_id:
            call_id = f"call_{round_index}_{call_index}"
        plain["id"] = call_id
        plain.setdefault("type", "function")
        function_payload = plain.get("function")
        if not isinstance(function_payload, dict):
            plain["function"] = {"name": "", "arguments": "{}"}
        else:
            function_payload.setdefault("arguments", "{}")
        normalised.append(plain)
    return normalised


def resolve_assistant_message(
    response: Any,
    *,
    provider: str,
    provider_name: str,
) -> tuple[Any, list[Any] | None]:
    choices = getattr(response, "choices", None) or []
    if not choices:
        raise RuntimeError(f"{provider_name} returned a response without choices")
    assistant_message = choices[0].message
    raw_tool_calls = getattr(assistant_message, "tool_calls", None)
    provider_spec = ai_providers.lookup(provider)
    if raw_tool_calls or provider_spec is None or not provider_spec.merges_split_tool_call_choices:
        return assistant_message, raw_tool_calls

    # Temporary compatibility for BerriAI/litellm#35444. Remove after the
    # deployed LiteLLM release merges mixed Responses output into one choice.
    for choice_index, choice in enumerate(choices[1:], start=1):
        candidate_message = getattr(choice, "message", None)
        candidate_tool_calls = getattr(candidate_message, "tool_calls", None)
        if candidate_tool_calls:
            logging.warning(
                "%s 返回的文本和工具调用被拆分到不同 choices；保留 choice 0 文本并使用 choice %s 的工具调用。",
                provider_name,
                choice_index,
            )
            return assistant_message, candidate_tool_calls

    return assistant_message, raw_tool_calls
