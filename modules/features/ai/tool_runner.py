import json
import logging
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .tools import OPENAI_TOOLS, AI_TOOL_HANDLERS
from .prompts import compose_system_prompt
from .types import AIResponse, ToolLog


def _tool_call_to_plain(tool_call: Any) -> Dict[str, Any]:
    """Normalize a tool call object into a plain JSON-serializable dict."""
    if isinstance(tool_call, dict):
        plain_call = dict(tool_call)
        function_payload = plain_call.get("function")
        if isinstance(function_payload, dict):
            plain_function = dict(function_payload)
            arguments = plain_function.get("arguments")
            if isinstance(arguments, (dict, list)):
                plain_function["arguments"] = json.dumps(arguments, ensure_ascii=False)
            elif arguments is None:
                plain_function["arguments"] = "{}"
            plain_call["function"] = plain_function
        return plain_call
    plain_call: Dict[str, Any] | None = None

    for attr in ("model_dump", "dict"):
        if hasattr(tool_call, attr):
            try:
                plain_call = getattr(tool_call, attr)()
            except TypeError:
                plain_call = getattr(tool_call, attr)(by_alias=True)
            except Exception:
                plain_call = None
            if isinstance(plain_call, dict):
                break

    if not isinstance(plain_call, dict):
        function = getattr(tool_call, "function", None)
        arguments = getattr(function, "arguments", None) if function else None
        if isinstance(arguments, (dict, list)):
            arguments_str = json.dumps(arguments, ensure_ascii=False)
        else:
            arguments_str = arguments if arguments is not None else "{}"

        return {
            "id": getattr(tool_call, "id", None),
            "type": getattr(tool_call, "type", "function"),
            "function": {
                "name": getattr(function, "name", None) if function else None,
                "arguments": arguments_str,
            },
        }

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

    return plain_call


def _normalise_tool_calls(tool_calls: Optional[List[Any]]) -> List[Dict[str, Any]]:
    if not tool_calls:
        return []
    return [_tool_call_to_plain(call) for call in tool_calls]


def _format_tool_fallback(payload: Tuple[str, Dict[str, Any]]) -> str:
    tool_name, tool_result = payload
    if tool_name == "google_search":
        results = tool_result.get("organic_results") or []
        if not results:
            return ""
        lines = ["以下是最新搜索结果："]
        for item in results[:3]:
            title = item.get("title") or "未命名结果"
            link = item.get("link") or ""
            snippet = item.get("snippet") or ""
            line = f"- {title}"
            if link:
                line += f" ({link})"
            if snippet:
                line += f"\n  {snippet}"
            lines.append(line)
        return "\n".join(lines)
    if tool_name == "fetch_group_context":
        messages = tool_result.get("messages") or []
        if not messages:
            return "未获取到群聊上下文。"
        lines = ["以下是当前消息之前的群聊记录："]
        for item in messages[:10]:
            timestamp = item.get("created_at") or ""
            username = item.get("username")
            if username:
                user_display = f"@{username}"
            else:
                user_display = f"用户 {item.get('user_id') or '未知'}"
            content = item.get("content") or ""
            mtype = item.get("message_type") or "text"
            lines.append(f"- [{timestamp}] {user_display} ({mtype}): {content}")
        return "\n".join(lines)
    return ""


def run_tool_loop(
    client: Any,
    model: str,
    messages: List[Dict[str, Any]],
    tool_context: Optional[Dict[str, object]] = None,
    *,
    provider_name: str = "AI",
    tool_choice: str | Dict[str, object] = "auto",
    temperature: float = 1.0,
    max_tokens: int = 4096,
    max_iterations: int = 10,
    skip_tools: Optional[Iterable[str]] = None,
) -> AIResponse:
    """Run a tool-calling loop for OpenAI-compatible chat.completions APIs."""
    tools = OPENAI_TOOLS
    system_message = {
        "role": "system",
        "content": compose_system_prompt(tool_context),
    }

    filtered_messages = [
        msg for msg in messages if msg.get("content") is not None or msg.get("tool_calls")
    ]
    filtered_messages.insert(0, system_message)

    last_tool_payload: Optional[Tuple[str, Dict[str, Any]]] = None
    tool_logs: List[ToolLog] = []
    skip_set = set(skip_tools or [])

    for iteration in range(max_iterations):
        if (
            iteration == 0
            and tool_context
            and tool_context.get("is_group")
            and tool_choice == "auto"
        ):
            request_tool_choice: str | Dict[str, object] = {
                "type": "function",
                "function": {"name": "fetch_group_context"},
            }
        else:
            request_tool_choice = tool_choice

        response = client.chat.completions.create(
            model=model,
            messages=filtered_messages,
            tools=tools,
            tool_choice=request_tool_choice,
            temperature=temperature,
            max_tokens=max_tokens,
        )

        assistant_message = response.choices[0].message
        raw_tool_calls = getattr(assistant_message, "tool_calls", None)
        assistant_content = assistant_message.content or ""

        if not raw_tool_calls:
            logging.info("%s 第 %s 轮：无工具调用，直接返回答案", provider_name, iteration + 1)
            content_text = assistant_content
            if content_text.strip():
                return content_text, tool_logs
            if last_tool_payload:
                fallback = _format_tool_fallback(last_tool_payload) or ""
                if fallback:
                    return fallback, tool_logs
                logging.warning("%s 返回内容为空且无可用回退。", provider_name)
            return content_text, tool_logs

        tool_calls = _normalise_tool_calls(raw_tool_calls)
        logging.info("%s 第 %s 轮：检测到 %s 个工具调用", provider_name, iteration + 1, len(tool_calls))

        filtered_messages.append({
            "role": "assistant",
            "content": assistant_content,
            "tool_calls": tool_calls,
        })

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
                function_args = json.loads(raw_args)
            except json.JSONDecodeError as exc:
                logging.error("%s 工具参数解析失败: %s", provider_name, exc)
                function_args = {}

            tool_call_id = tool_call.get("id")
            tool_logs.append({
                "type": "assistant_tool_call",
                "tool_name": function_name,
                "arguments": function_args,
                "tool_call_id": tool_call_id,
            })

            handler = AI_TOOL_HANDLERS.get(function_name)
            if handler:
                try:
                    tool_result = handler(**function_args)
                    logging.info(
                        "%s 工具执行成功: %s, args=%s",
                        provider_name,
                        function_name,
                        json.dumps(function_args, ensure_ascii=False),
                    )
                except TypeError as exc:
                    logging.error("%s 工具参数错误: %s, %s", provider_name, function_name, exc)
                    tool_result = {"error": f"参数错误: {str(exc)}"}
                except Exception as exc:
                    logging.exception("%s 工具执行失败: %s, %s", provider_name, function_name, exc)
                    tool_result = {"error": f"执行失败: {str(exc)}"}
            else:
                logging.warning("%s 未知工具: %s", provider_name, function_name)
                tool_result = {"error": f"未知工具: {function_name}"}

            filtered_messages.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "name": function_name,
                "content": json.dumps(tool_result, ensure_ascii=False),
            })
            last_tool_payload = (function_name, tool_result)
            tool_logs.append({
                "type": "tool_result",
                "tool_name": function_name,
                "arguments": function_args,
                "result": tool_result,
                "tool_call_id": tool_call_id,
            })

    logging.warning("%s 工具调用次数超限（%s轮）", provider_name, max_iterations)
    return "抱歉，处理您的请求时遇到了问题，请稍后再试。", tool_logs
