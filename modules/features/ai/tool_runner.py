import base64
import json
import logging
from typing import Any, Dict, Iterable, List, NamedTuple, Optional

from pydantic import ValidationError

from .tools import OPENAI_TOOLS, AI_TOOL_ARG_MODELS, AI_TOOL_HANDLERS
from .prompts import compose_system_prompt
from .litellm_client import create_chat_completion
from .types import AIResponse, PartialAIResponseError, ToolLog, VisibleContentHandler


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
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
                return _json_safe(dumped)
    return str(value)


def _drop_none_items(value: Dict[str, Any]) -> Dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None}


def _format_validation_errors(exc: ValidationError) -> list[dict[str, str]]:
    details: list[dict[str, str]] = []
    for error in exc.errors(include_url=False):
        loc = ".".join(str(item) for item in error.get("loc", ())) or "__root__"
        details.append({
            "field": loc,
            "message": str(error.get("msg") or "Invalid value"),
            "type": str(error.get("type") or "validation_error"),
        })
    return details


def _validate_tool_args(
    function_name: str,
    raw_args: Any,
) -> tuple[Dict[str, Any], Dict[str, Any] | None]:
    model = AI_TOOL_ARG_MODELS.get(function_name)
    if model is None:
        if isinstance(raw_args, dict):
            return raw_args, None
        return {}, None

    try:
        validated = model.model_validate(raw_args)
    except ValidationError as exc:
        return {}, {
            "error": "Tool arguments failed validation",
            "details": _format_validation_errors(exc),
        }

    return validated.model_dump(
        mode="json",
        exclude_none=True,
        exclude_unset=True,
    ), None


def _tool_call_to_plain(tool_call: Any) -> Dict[str, Any]:
    """Normalize a tool call object into a plain JSON-serializable dict."""
    if isinstance(tool_call, dict):
        plain_call = _json_safe(dict(tool_call))
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
    plain_call: Dict[str, Any] | None = None

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
                plain_call = _json_safe(plain_call)
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
            plain_call["provider_specific_fields"] = _json_safe(provider_specific_fields)
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


def _message_to_plain_dict(message: Any) -> Dict[str, Any]:
    if isinstance(message, dict):
        return _drop_none_items(_json_safe(dict(message)))

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
                    return _drop_none_items(_json_safe(dumped))

    result: Dict[str, Any] = {}
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
            result[key] = _json_safe(value)
    return _drop_none_items(result)


def _assistant_message_to_plain(
    assistant_message: Any,
    *,
    content: str,
    tool_calls: List[Dict[str, Any]],
) -> Dict[str, Any]:
    message = _message_to_plain_dict(assistant_message)
    message["role"] = "assistant"
    message["content"] = content
    if tool_calls:
        message["tool_calls"] = tool_calls
    else:
        message.pop("tool_calls", None)
    return message


def _normalise_tool_calls(tool_calls: Optional[List[Any]]) -> List[Dict[str, Any]]:
    if not tool_calls:
        return []
    return [_tool_call_to_plain(call) for call in tool_calls]


def _public_tool_result(
    tool_name: str,
    tool_result: Dict[str, Any],
    *,
    media_sent: bool = False,
) -> Dict[str, Any]:
    if tool_name not in {"generate_image", "generate_voice"} or not isinstance(tool_result, dict):
        return tool_result

    if tool_result.get("error"):
        public_result = {"error": tool_result.get("error")}
        for key in ("status_code", "details", "response_preview", "warnings", "retry_after_seconds"):
            if key in tool_result:
                public_result[key] = tool_result[key]
        return public_result

    if tool_name == "generate_image" and tool_result.get("status") == "generated":
        return {
            "status": "generated",
            "message": (
                "Generated image has been sent to Telegram."
                if media_sent
                else (
                    "Generated image is ready and will be sent to Telegram. "
                    "If you need to inspect the image yourself later, ask the user "
                    "to forward the sent image back to you."
                )
            ),
        }

    if tool_name == "generate_voice" and tool_result.get("status") == "generated":
        return {
            "status": "generated",
            "message": (
                "Generated audio has been sent to Telegram."
                if media_sent
                else "Generated audio is ready and will be sent to Telegram."
            ),
        }

    return {"status": tool_result.get("status") or "unknown"}


def _log_generate_image_result(provider_name: str, tool_result: Dict[str, Any]) -> None:
    if not isinstance(tool_result, dict):
        logging.warning("%s generate_image returned non-dict result: %s", provider_name, type(tool_result).__name__)
        return

    if tool_result.get("error"):
        logging.warning(
            "%s generate_image returned error: error=%s, status_code=%s, retry_after_seconds=%s, details=%s",
            provider_name,
            tool_result.get("error"),
            tool_result.get("status_code"),
            tool_result.get("retry_after_seconds"),
            str(tool_result.get("details") or tool_result.get("response_preview") or "")[:500],
        )
        return

    if tool_result.get("status") == "generated":
        images = tool_result.get("images") if isinstance(tool_result.get("images"), list) else []
        logging.info(
            "%s generate_image generated %s image(s): count=%s, warnings=%s",
            provider_name,
            len(images),
            tool_result.get("count"),
            tool_result.get("warnings"),
        )
        return

    logging.info(
        "%s generate_image returned status=%s",
        provider_name,
        tool_result.get("status") or "unknown",
    )


def _log_generate_voice_result(provider_name: str, tool_result: Dict[str, Any]) -> None:
    if not isinstance(tool_result, dict):
        logging.warning("%s generate_voice returned non-dict result: %s", provider_name, type(tool_result).__name__)
        return

    if tool_result.get("error"):
        logging.warning(
            "%s generate_voice returned error: error=%s, status_code=%s, retry_after_seconds=%s, details=%s",
            provider_name,
            tool_result.get("error"),
            tool_result.get("status_code"),
            tool_result.get("retry_after_seconds"),
            str(tool_result.get("details") or tool_result.get("response_preview") or "")[:500],
        )
        return

    if tool_result.get("status") == "generated":
        audios = tool_result.get("audios") if isinstance(tool_result.get("audios"), list) else []
        logging.info(
            "%s generate_voice generated %s audio clip(s): count=%s, warnings=%s",
            provider_name,
            len(audios),
            tool_result.get("count"),
            tool_result.get("warnings"),
        )
        return

    logging.info(
        "%s generate_voice returned status=%s",
        provider_name,
        tool_result.get("status") or "unknown",
    )


def _send_media_result_immediately(
    *,
    visible_content_handler: Optional[VisibleContentHandler],
    tool_name: str,
    tool_result: Dict[str, Any],
    provider_name: str,
) -> list[Any]:
    if visible_content_handler is None:
        return []
    if tool_name not in {"generate_image", "generate_voice"}:
        return []
    if not isinstance(tool_result, dict) or tool_result.get("status") != "generated":
        return []

    send_tool_media = getattr(visible_content_handler, "send_tool_media", None)
    if not callable(send_tool_media):
        return []

    try:
        sent_messages = send_tool_media(tool_name, tool_result)
    except Exception as exc:
        logging.exception("%s failed to send %s result immediately: %s", provider_name, tool_name, exc)
        return []

    if not isinstance(sent_messages, list):
        return []
    return sent_messages


class _VisibleContentResult(NamedTuple):
    content: str
    completed: bool


def _last_visible_content(handler: VisibleContentHandler) -> str:
    visible_events = getattr(handler, "visible_events", None)
    if callable(visible_events):
        try:
            events = visible_events()
            if isinstance(events, list):
                for event in reversed(events):
                    if not isinstance(event, dict):
                        continue
                    content = str(event.get("content") or "").strip()
                    if content:
                        return content
        except Exception:
            logging.exception("Failed to read visible content events")

    contents = getattr(handler, "sent_contents", [])
    if not isinstance(contents, list):
        return ""
    for content in reversed(contents):
        normalized = str(content or "").strip()
        if normalized:
            return normalized
    return ""


def _emit_visible_content(
    handler: VisibleContentHandler,
    content: str,
    *,
    provider_name: str,
) -> _VisibleContentResult:
    """Send visible assistant content through the host app and return what was sent."""
    if not content.strip():
        return _VisibleContentResult("", True)

    try:
        visible_content = handler(content)
    except Exception as exc:
        logging.exception("%s visible content handler failed: %s", provider_name, exc)
        partial_content = _last_visible_content(handler)
        if partial_content:
            return _VisibleContentResult(partial_content, False)
        return _VisibleContentResult("", True)

    if visible_content is None:
        partial_content = _last_visible_content(handler)
        if partial_content:
            return _VisibleContentResult(partial_content, False)
        return _VisibleContentResult("", True)
    normalized = str(visible_content).strip()
    if not normalized:
        partial_content = _last_visible_content(handler)
        if partial_content:
            return _VisibleContentResult(partial_content, False)
    return _VisibleContentResult(normalized, True)


def run_tool_loop(
    provider: str,
    model: str,
    messages: List[Dict[str, Any]],
    tool_context: Optional[Dict[str, object]] = None,
    *,
    provider_name: str = "AI",
    tool_choice: str | Dict[str, object] = "auto",
    max_tokens: int = 4096,
    max_iterations: int = 10,
    skip_tools: Optional[Iterable[str]] = None,
    completion_kwargs: Optional[Dict[str, Any]] = None,
    visible_content_handler: Optional[VisibleContentHandler] = None,
) -> AIResponse:
    """Run a tool-calling loop through LiteLLM using OpenAI-format tools."""
    tools = OPENAI_TOOLS
    system_message = {
        "role": "system",
        "content": compose_system_prompt(tool_context),
    }

    filtered_messages = [
        msg for msg in messages if msg.get("content") is not None or msg.get("tool_calls")
    ]
    filtered_messages.insert(0, system_message)

    tool_logs: List[ToolLog] = []
    skip_set = set(skip_tools or [])

    for iteration in range(max_iterations):
        request_tool_choice = tool_choice
        try:
            request_kwargs = {"max_tokens": max_tokens, **(completion_kwargs or {})}
            response = create_chat_completion(
                provider,
                model,
                messages=filtered_messages,
                tools=tools,
                tool_choice=request_tool_choice,
                **request_kwargs,
            )
        except Exception as exc:
            if tool_logs:
                raise PartialAIResponseError(str(exc), tool_logs) from exc
            raise

        assistant_message = response.choices[0].message
        raw_tool_calls = getattr(assistant_message, "tool_calls", None)
        assistant_content = assistant_message.content or ""

        if not raw_tool_calls:
            logging.info("%s 第 %s 轮：无工具调用，直接返回答案", provider_name, iteration + 1)
            content_text = assistant_content
            if content_text.strip():
                if visible_content_handler:
                    visible_result = _emit_visible_content(
                        visible_content_handler,
                        content_text,
                        provider_name=provider_name,
                    )
                    if visible_result.content:
                        tool_logs.append({
                            "type": "assistant_visible",
                            "content": visible_result.content,
                        })
                        return "", tool_logs
                    if not visible_result.completed:
                        return "", tool_logs
                    return content_text, tool_logs
                return content_text, tool_logs
            if tool_logs:
                logging.warning("%s 工具调用后最终回复为空。", provider_name)
            return content_text, tool_logs

        tool_calls = _normalise_tool_calls(raw_tool_calls)
        logging.info("%s 第 %s 轮：检测到 %s 个工具调用", provider_name, iteration + 1, len(tool_calls))

        assistant_content_for_model = assistant_content
        if visible_content_handler and assistant_content.strip():
            visible_result = _emit_visible_content(
                visible_content_handler,
                assistant_content,
                provider_name=provider_name,
            )
            if visible_result.content:
                assistant_content_for_model = visible_result.content
                tool_logs.append({
                    "type": "assistant_visible",
                    "content": visible_result.content,
                })
                if not visible_result.completed:
                    return "", tool_logs
            elif not visible_result.completed:
                return "", tool_logs

        assistant_model_message = _assistant_message_to_plain(
            assistant_message,
            content=assistant_content_for_model,
            tool_calls=tool_calls,
        )
        filtered_messages.append(assistant_model_message)

        assistant_message_logged = False
        for tool_call in tool_calls:
            function_payload = tool_call.get("function") or {}
            function_name = function_payload.get("name")
            if not function_name:
                logging.warning("%s 返回的工具调用缺少函数名: %s", provider_name, tool_call)
                continue

            if function_name in skip_set:
                continue

            raw_args = function_payload.get("arguments") or "{}"
            try:
                raw_function_args = json.loads(raw_args)
            except json.JSONDecodeError as exc:
                logging.error("%s 工具参数解析失败: %s", provider_name, exc)
                raw_function_args = {}

            function_args, validation_error = _validate_tool_args(
                function_name,
                raw_function_args,
            )
            logged_args = (
                function_args
                if validation_error is None
                else _json_safe(raw_function_args)
            )

            tool_call_id = tool_call.get("id")
            tool_log_entry = {
                "type": "assistant_tool_call",
                "tool_name": function_name,
                "arguments": logged_args,
                "tool_call_id": tool_call_id,
            }
            if validation_error is not None:
                tool_log_entry["validation_error"] = validation_error
            if not assistant_message_logged:
                tool_log_entry["assistant_message"] = assistant_model_message
                assistant_message_logged = True
            tool_logs.append(tool_log_entry)

            handler = AI_TOOL_HANDLERS.get(function_name)
            if validation_error is not None:
                logging.warning(
                    "%s 工具参数校验失败: %s, args=%s, error=%s",
                    provider_name,
                    function_name,
                    json.dumps(_json_safe(raw_function_args), ensure_ascii=False),
                    validation_error.get("details"),
                )
                internal_tool_result = validation_error
            elif handler:
                try:
                    internal_tool_result = handler(**function_args)
                    if isinstance(internal_tool_result, dict) and internal_tool_result.get("error"):
                        logging.warning(
                            "%s 工具返回错误: %s, args=%s, error=%s",
                            provider_name,
                            function_name,
                            json.dumps(function_args, ensure_ascii=False),
                            internal_tool_result.get("error"),
                        )
                    else:
                        logging.info(
                            "%s 工具执行成功: %s, args=%s",
                            provider_name,
                            function_name,
                            json.dumps(function_args, ensure_ascii=False),
                        )
                except TypeError as exc:
                    logging.error("%s 工具参数错误: %s, %s", provider_name, function_name, exc)
                    internal_tool_result = {"error": f"参数错误: {str(exc)}"}
                except Exception as exc:
                    logging.exception("%s 工具执行失败: %s, %s", provider_name, function_name, exc)
                    internal_tool_result = {"error": f"执行失败: {str(exc)}"}
            else:
                logging.warning("%s 未知工具: %s", provider_name, function_name)
                internal_tool_result = {"error": f"未知工具: {function_name}"}

            if function_name == "generate_image":
                _log_generate_image_result(provider_name, internal_tool_result)
            elif function_name == "generate_voice":
                _log_generate_voice_result(provider_name, internal_tool_result)

            sent_media_messages = _send_media_result_immediately(
                visible_content_handler=visible_content_handler,
                tool_name=function_name,
                tool_result=internal_tool_result,
                provider_name=provider_name,
            )
            media_sent = bool(sent_media_messages)

            tool_result = _public_tool_result(
                function_name,
                internal_tool_result,
                media_sent=media_sent,
            )

            filtered_messages.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "name": function_name,
                "content": json.dumps(tool_result, ensure_ascii=False),
            })
            tool_log_entry = {
                "type": "tool_result",
                "tool_name": function_name,
                "arguments": function_args,
                "result": tool_result,
                "tool_call_id": tool_call_id,
            }
            if function_name in {"generate_image", "generate_voice"}:
                tool_log_entry["internal_result"] = internal_tool_result
                if media_sent:
                    tool_log_entry["media_sent"] = True
                    tool_log_entry["sent_message_count"] = len(sent_media_messages)
            tool_logs.append(tool_log_entry)

    logging.warning("%s 工具调用次数超限（%s轮）", provider_name, max_iterations)
    return "抱歉，处理您的请求时遇到了问题，请稍后再试。", tool_logs
