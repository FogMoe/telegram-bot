import logging
from typing import Dict, Optional, List, Tuple, Any

from openai import OpenAI
from core import config
import json
from collections import deque
import time
import asyncio
from concurrent.futures import ThreadPoolExecutor

from .ai_tools import (
    OPENAI_TOOLS,
    AI_TOOL_HANDLERS,
    set_tool_request_context,
    clear_tool_request_context,
)

# 创建线程池执行器用于异步调用阻塞式API
executor = ThreadPoolExecutor(max_workers=10)

ToolLog = Dict[str, Any]
AIResponse = Tuple[str, List[ToolLog]]


SYSTEM_PROMPT = config.SYSTEM_PROMPT
AI_SERVICE_ORDER = config.AI_SERVICE_ORDER
ZAI_API_KEY = config.ZAI_API_KEY

# 限速器实现
class APIRateLimiter:
    def __init__(self, max_requests=10, time_window=60):  # 默认1分钟10次请求
        self.requests = deque()
        self.max_requests = max_requests
        self.time_window = time_window
    
    def can_make_request(self):
        now = time.time()
        # 清理过期请求记录
        while self.requests and now - self.requests[0] > self.time_window:
            self.requests.popleft()
        
        if len(self.requests) < self.max_requests:
            self.requests.append(now)
            return True
        return False

# 创建全局限速器实例
translate_limiter = APIRateLimiter(max_requests=10, time_window=60)


def _build_openai_client(
    api_key: str,
    *,
    base_url: Optional[str] = None,
    default_headers: Optional[Dict[str, str]] = None,
    default_query: Optional[Dict[str, str]] = None,
) -> OpenAI:
    if not api_key:
        raise RuntimeError("Missing API key configuration.")
    client_kwargs: Dict[str, Any] = {"api_key": api_key}
    if base_url:
        client_kwargs["base_url"] = base_url
    if default_headers:
        client_kwargs["default_headers"] = default_headers
    if default_query:
        client_kwargs["default_query"] = default_query
    return OpenAI(**client_kwargs)


def create_zhipu_client() -> OpenAI:
    if not ZAI_API_KEY:
        raise RuntimeError("Missing ZAI_API_KEY configuration.")
    return _build_openai_client(
        ZAI_API_KEY,
        base_url=config.ZHIPU_BASE_URL,
    )


def create_gemini_client() -> OpenAI:
    if not config.GEMINI_API_KEY:
        raise RuntimeError("Missing GEMINI_API_KEY configuration.")
    return _build_openai_client(
        config.GEMINI_API_KEY,
        base_url=config.GEMINI_BASE_URL,
    )


def create_azure_client() -> OpenAI:
    if not config.AZURE_OPENAI_API_KEY:
        raise RuntimeError("Missing AZURE_OPENAI_API_KEY configuration.")
    if not config.AZURE_OPENAI_BASE_URL:
        raise RuntimeError("Missing AZURE_OPENAI_BASE_URL configuration.")
    default_query = {"api-version": config.AZURE_OPENAI_API_VERSION}
    default_headers = {"api-key": config.AZURE_OPENAI_API_KEY}
    return _build_openai_client(
        config.AZURE_OPENAI_API_KEY,
        base_url=config.AZURE_OPENAI_BASE_URL,
        default_headers=default_headers,
        default_query=default_query,
    )


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

    function = getattr(tool_call, "function", None)
    arguments = getattr(function, "arguments", None) if function else None
    if isinstance(arguments, (dict, list)):
        arguments_str = json.dumps(arguments, ensure_ascii=False)
    else:
        arguments_str = arguments if arguments is not None else "{}"

    plain = {
        "id": getattr(tool_call, "id", None),
        "type": getattr(tool_call, "type", "function"),
        "function": {
            "name": getattr(function, "name", None) if function else None,
            "arguments": arguments_str,
        },
    }
    return plain


def _normalise_tool_calls(tool_calls: Optional[List[Any]]) -> List[Dict[str, Any]]:
    if not tool_calls:
        return []
    return [_tool_call_to_plain(call) for call in tool_calls]

async def translate_text(text: str) -> str:
    """专门用于文本翻译的AI函数（异步版本）"""
    try:
        if not translate_limiter.can_make_request():
            return "请求过于频繁，请稍后再试。\nToo many requests, please try again later."
        
        loop = asyncio.get_running_loop()
        # 使用线程池执行阻塞的API调用
        return await loop.run_in_executor(
            executor, 
            lambda: _sync_translate_text(text)
        )
    except Exception as e:
        logging.error(f"翻译过程中出错: {str(e)}")
        return "翻译失败，请稍后重试。\nTranslation failed, please try again later."


def _sync_translate_text(text: str) -> str:
    """同步版本的翻译函数，供异步函数调用"""
    client = create_zhipu_client()
    response = client.chat.completions.create(
        model=config.ZHIPU_TRANSLATE_MODEL,
        messages=[
            {
                "role": "system",
                "content": "You are a professional translation assistant. If the user enters Chinese, please translate it into English; if the user enters English, please translate it into Chinese. The translation should be colloquial, cat-girl like, cute, and adorable."
            },
            {
                "role": "user",
                "content": f"Only output the final translated text, please translate the following text: \n{text}"
            }
        ]
    )
    return response.choices[0].message.content


async def analyze_image(base64_str):
    """调用 Z.ai 对图像进行分析并返回描述文本（异步版本）"""
    try:
        if not base64_str:
            raise ValueError("Image data is empty.")
            
        loop = asyncio.get_running_loop()
        # 使用线程池执行阻塞的API调用
        return await loop.run_in_executor(
            executor,
            lambda: _sync_analyze_image(base64_str)
        )

    except ValueError as e:
        logging.error(f"图片数据验证失败: {str(e)}")
        return "Image validation failed, please check the image format."
        
    except ConnectionError as e:
        logging.error(f"连接 Z.ai 服务失败: {str(e)}")
        return "Failed to connect to AI service."
        
    except Exception as e:
        logging.error(f"处理图片时发生未知错误: {str(e)}")
        return "An error occurred while processing the image."


def _sync_analyze_image(base64_str):
    """同步版本的图像分析函数，供异步函数调用"""
    client = create_zhipu_client()
    response = client.chat.completions.create(
        model=config.ZHIPU_VISION_MODEL,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": base64_str
                        }
                    },
                    {
                        "type": "text",
                        "text": "Please provide a detailed description of this image, including main objects, scene, actions, colors, atmosphere and other key elements. If it's an emoji or sticker, please explain its emotional expression and meaning. Use clear and concise language in your description."
                    }
                ]
            }
        ]
    )
    return response.choices[0].message.content


def _compose_system_prompt(
    tool_context: Optional[Dict[str, object]],
) -> str:
    """Return the base system prompt with any dynamic additions."""
    extra_prompt = ""
    if tool_context:
        dynamic_hint = tool_context.get("user_state_prompt")
        if dynamic_hint:
            extra_prompt = f"{dynamic_hint}"
    return SYSTEM_PROMPT + extra_prompt


def get_ai_response_zhipu(messages, user_id: int, tool_context: Optional[Dict[str, object]] = None) -> AIResponse:
    """同步版本的 Z.ai（原智谱）响应函数（支持工具调用）"""
    client = create_zhipu_client()

    # 获取 OpenAI 格式的工具定义
    tools = OPENAI_TOOLS

    # 添加系统消息
    system_message = {
        "role": "system",
        "content": _compose_system_prompt(tool_context),
    }
    
    # 过滤掉完全空的消息，但保留带 tool_calls 的助手消息
    filtered_messages = [
        msg for msg in messages if msg.get("content") is not None or msg.get("tool_calls")
    ]
    filtered_messages.insert(0, system_message)

    last_tool_payload = None
    tool_logs: List[ToolLog] = []

    # 工具调用循环（最多10轮）
    for iteration in range(10):
        response = client.chat.completions.create(
            model=config.ZHIPU_MODEL,
            messages=filtered_messages,
            tools=tools,
            tool_choice="auto",
            temperature=1.0,
            max_tokens=4096,
        )
        
        assistant_message = response.choices[0].message
        raw_tool_calls = getattr(assistant_message, 'tool_calls', None)
        assistant_content = assistant_message.content or ""
        
        # 如果没有工具调用，直接返回当前模型答案
        if not raw_tool_calls:
            logging.info(f"Z.ai 第 {iteration + 1} 轮：无工具调用，直接返回答案")
            content_text = assistant_content
            if content_text.strip():
                return content_text, tool_logs
            if last_tool_payload:
                fallback = _format_tool_fallback(last_tool_payload) or ""
                if fallback:
                    return fallback, tool_logs
                logging.warning("Z.ai 返回内容为空且无可用回退。")
            return content_text, tool_logs
        
        tool_calls = _normalise_tool_calls(raw_tool_calls)
        logging.info(f"Z.ai 第 {iteration + 1} 轮：检测到 {len(tool_calls)} 个工具调用")
        
        # 追加助手消息（包含 tool_calls）
        filtered_messages.append({
            "role": "assistant",
            "content": assistant_content,
            "tool_calls": tool_calls
        })
        
        # 执行所有工具调用
        for tool_call in tool_calls:
            function_payload = tool_call.get("function") or {}
            function_name = function_payload.get("name")
            if not function_name:
                logging.warning("Z.ai 返回的工具调用缺少函数名: %s", tool_call)
                continue

            # 跳过 Z.ai 内置工具（web_search, web_browser）
            if function_name in ["web_search", "web_browser"]:
                continue

            raw_args = function_payload.get("arguments") or "{}"
            try:
                function_args = json.loads(raw_args)
            except json.JSONDecodeError as e:
                logging.error(f"Z.ai 工具参数解析失败: {e}")
                function_args = {}

            tool_call_id = tool_call.get("id")
            tool_logs.append({
                "type": "assistant_tool_call",
                "tool_name": function_name,
                "arguments": function_args,
                "tool_call_id": tool_call_id,
            })

            # 执行工具
            handler = AI_TOOL_HANDLERS.get(function_name)
            if handler:
                try:
                    tool_result = handler(**function_args)
                    logging.info(
                        f"Z.ai 工具执行成功: {function_name}, "
                        f"args={json.dumps(function_args, ensure_ascii=False)}"
                    )
                except TypeError as e:
                    logging.error(f"Z.ai 工具参数错误: {function_name}, {e}")
                    tool_result = {"error": f"参数错误: {str(e)}"}
                except Exception as e:
                    logging.exception(f"Z.ai 工具执行失败: {function_name}, {e}")
                    tool_result = {"error": f"执行失败: {str(e)}"}
            else:
                logging.warning(f"Z.ai 未知工具: {function_name}")
                tool_result = {"error": f"未知工具: {function_name}"}
            
            # 追加工具返回结果
            filtered_messages.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "name": function_name,
                "content": json.dumps(tool_result, ensure_ascii=False)
            })
            last_tool_payload = (function_name, tool_result)
            tool_logs.append({
                "type": "tool_result",
                "tool_name": function_name,
                "arguments": function_args,
                "result": tool_result,
                "tool_call_id": tool_call_id,
            })

    logging.warning("Z.ai 工具调用次数超限（10轮）")
    return "抱歉，处理您的请求时遇到了问题，请稍后再试。", tool_logs


def get_ai_response_azure(messages, user_id: int, tool_context: Optional[Dict[str, object]] = None) -> AIResponse:
    """同步版本的Azure OpenAI响应函数（支持工具调用）"""
    client = create_azure_client()
    azure_model = config.AZURE_OPENAI_DEPLOYMENT or "gpt-4"

    # 获取 OpenAI 格式的工具定义
    tools = OPENAI_TOOLS

    # 添加系统消息
    system_message = {
        "role": "system",
        "content": _compose_system_prompt(tool_context)
    }
    
    # 过滤掉完全空的消息，但保留带 tool_calls 的助手消息
    filtered_messages = [
        msg for msg in messages if msg.get("content") is not None or msg.get("tool_calls")
    ]
    filtered_messages.insert(0, system_message)

    last_tool_payload = None
    tool_logs: List[ToolLog] = []

    try:
        # 工具调用循环（最多10轮）
        for iteration in range(10):
            completion = client.chat.completions.create(
                model=azure_model,
                messages=filtered_messages,
                tools=tools,
                tool_choice="auto",
                temperature=1.0,
                max_tokens=4096,
            )
            
            assistant_message = completion.choices[0].message
            raw_tool_calls = getattr(assistant_message, 'tool_calls', None)
            assistant_content = assistant_message.content or ""
            
            # 如果没有工具调用，直接返回当前模型答案
            if not raw_tool_calls:
                logging.info(f"Azure 第 {iteration + 1} 轮：无工具调用，直接返回答案")
                content_text = assistant_content
                if content_text.strip():
                    return content_text, tool_logs
                if last_tool_payload:
                    fallback = _format_tool_fallback(last_tool_payload) or ""
                    if fallback:
                        return fallback, tool_logs
                logging.warning("Azure 返回内容为空且无可用回退。")
                return content_text, tool_logs
            
            tool_calls = _normalise_tool_calls(raw_tool_calls)
            logging.info(f"Azure 第 {iteration + 1} 轮：检测到 {len(tool_calls)} 个工具调用")
            
            # 追加助手消息（包含 tool_calls）
            filtered_messages.append({
                "role": "assistant",
                "content": assistant_content,
                "tool_calls": tool_calls
            })
            
            # 执行所有工具调用
            for tool_call in tool_calls:
                function_payload = tool_call.get("function") or {}
                function_name = function_payload.get("name")
                if not function_name:
                    logging.warning("Azure 返回的工具调用缺少函数名: %s", tool_call)
                    continue

                try:
                    raw_args = function_payload.get("arguments") or "{}"
                    function_args = json.loads(raw_args)
                except json.JSONDecodeError as e:
                    logging.error(f"Azure 工具参数解析失败: {e}")
                    function_args = {}

                tool_logs.append({
                    "type": "assistant_tool_call",
                    "tool_name": function_name,
                    "arguments": function_args,
                    "tool_call_id": tool_call.get("id"),
                })

                # 执行工具
                handler = AI_TOOL_HANDLERS.get(function_name)
                if handler:
                    try:
                        tool_result = handler(**function_args)
                        logging.info(
                            f"Azure 工具执行成功: {function_name}, "
                            f"args={json.dumps(function_args, ensure_ascii=False)}"
                        )
                    except TypeError as e:
                        logging.error(f"Azure 工具参数错误: {function_name}, {e}")
                        tool_result = {"error": f"参数错误: {str(e)}"}
                    except Exception as e:
                        logging.exception(f"Azure 工具执行失败: {function_name}, {e}")
                        tool_result = {"error": f"执行失败: {str(e)}"}
                else:
                    logging.warning(f"Azure 未知工具: {function_name}")
                    tool_result = {"error": f"未知工具: {function_name}"}
                
                # 追加工具返回结果
                filtered_messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.get("id"),
                    "name": function_name,
                    "content": json.dumps(tool_result, ensure_ascii=False)
                })
                last_tool_payload = (function_name, tool_result)
                tool_logs.append({
                    "type": "tool_result",
                    "tool_name": function_name,
                    "arguments": function_args,
                    "result": tool_result,
                    "tool_call_id": tool_call.get("id"),
                })
        
        logging.warning("Azure 工具调用次数超限（10轮）")
        return "抱歉，处理您的请求时遇到了问题，请稍后再试。", tool_logs
        
    except Exception as e:
        logging.error(f"Azure OpenAI 请求失败: {e}")
        raise


def get_ai_response_google(messages, user_id: int, tool_context: Optional[Dict[str, object]] = None) -> AIResponse:
    """同步版本的Google Gemini响应函数（OpenAI兼容接口）。"""
    client = create_gemini_client()
    tools = OPENAI_TOOLS

    system_message = {
        "role": "system",
        "content": _compose_system_prompt(tool_context),
    }

    filtered_messages = [
        msg for msg in messages if msg.get("content") is not None or msg.get("tool_calls")
    ]
    filtered_messages.insert(0, system_message)

    last_tool_payload = None
    tool_logs: List[ToolLog] = []

    def _run(model_name: str) -> AIResponse:
        nonlocal last_tool_payload
        for iteration in range(10):
            response = client.chat.completions.create(
                model=model_name,
                messages=filtered_messages,
                tools=tools,
                tool_choice="auto",
                temperature=1.0,
                max_tokens=4096,
            )

            assistant_message = response.choices[0].message
            raw_tool_calls = getattr(assistant_message, "tool_calls", None)
            assistant_content = assistant_message.content or ""

            if not raw_tool_calls:
                logging.info(f"Gemini 第 {iteration + 1} 轮：无工具调用，直接返回答案")
                content_text = assistant_content
                if content_text.strip():
                    return content_text, tool_logs
                if last_tool_payload:
                    fallback = _format_tool_fallback(last_tool_payload) or ""
                    if fallback:
                        return fallback, tool_logs
                logging.warning("Gemini 返回内容为空且无可用回退。")
                return content_text, tool_logs

            tool_calls = _normalise_tool_calls(raw_tool_calls)
            logging.info(f"Gemini 第 {iteration + 1} 轮：检测到 {len(tool_calls)} 个工具调用")

            filtered_messages.append({
                "role": "assistant",
                "content": assistant_content,
                "tool_calls": tool_calls,
            })

            for tool_call in tool_calls:
                function_payload = tool_call.get("function") or {}
                function_name = function_payload.get("name")
                if not function_name:
                    logging.warning("Gemini 返回的工具调用缺少函数名: %s", tool_call)
                    continue

                raw_args = function_payload.get("arguments") or "{}"
                try:
                    function_args = json.loads(raw_args)
                except json.JSONDecodeError as e:
                    logging.error(f"Gemini 工具参数解析失败: {e}")
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
                            f"Gemini 工具执行成功: {function_name}, "
                            f"args={json.dumps(function_args, ensure_ascii=False)}"
                        )
                    except TypeError as e:
                        logging.error(f"Gemini 工具参数错误: {function_name}, {e}")
                        tool_result = {"error": f"参数错误: {str(e)}"}
                    except Exception as e:
                        logging.exception(f"Gemini 工具执行失败: {function_name}, {e}")
                        tool_result = {"error": f"执行失败: {str(e)}"}
                else:
                    logging.warning(f"Gemini 未知工具: {function_name}")
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

        logging.warning("Gemini 工具调用次数超限（10轮）")
        return "抱歉，处理您的请求时遇到了问题，请稍后再试。", tool_logs

    primary_model = config.GEMINI_MODEL
    fallback_model = config.GEMINI_FALLBACK_MODEL
    try:
        return _run(primary_model)
    except Exception as e:
        error_str = str(e)
        if fallback_model and fallback_model != primary_model:
            logging.warning(
                "Gemini 主模型失败，尝试回退模型 %s: %s",
                fallback_model,
                error_str,
            )
            return _run(fallback_model)
        if "SAFETY" in error_str and "blocked" in error_str:
            logging.warning("Gemini safety block triggered: %s", error_str)
            raise Exception("SafetyBlockError")

        logging.error(f"Google Gemini 请求失败: {error_str}")
        raise
            

AI_SERVICE_MAP = {
    "gemini": get_ai_response_google,
    "azure": get_ai_response_azure,
    "zhipu": get_ai_response_zhipu
}


def _call_service_with_context(service_name: str, messages, user_id: int, tool_context: Optional[Dict[str, object]]) -> AIResponse:
    set_tool_request_context(dict(tool_context or {}))
    try:
        return AI_SERVICE_MAP[service_name](messages, user_id, tool_context)
    finally:
        clear_tool_request_context()


async def get_ai_response(messages, user_id: int, tool_context: Optional[Dict[str, object]] = None) -> AIResponse:
    """
    统一AI响应异步接口，根据配置的顺序依次尝试不同的AI服务
    """
    last_error = None
    loop = asyncio.get_running_loop()

    for service_name in AI_SERVICE_ORDER:
        try:
            # 使用线程池执行阻塞的API调用
            response = await loop.run_in_executor(
                executor,
                lambda s=service_name: _call_service_with_context(
                    s, messages.copy(), user_id, tool_context
                )
            )
            return response
        except Exception as e:
            if service_name == "gemini" and "SafetyBlockError" in str(e):
                logging.warning("Gemini triggered safety block, trying next service")
            else:
                logging.warning(f"{service_name} 调用失败: {e}")
            last_error = e
            continue

    logging.error(f"所有AI服务均调用失败: {last_error}")
    return (
        "抱歉喵，雾萌娘在处理你的请求时遇到了一点小问题！现在有点不舒服啦，请稍后再试吧～\n请联系管理员 @ScarletKc 反馈问题。",
        [],
    )


def _format_tool_fallback(payload):
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
