from zhipuai import ZhipuAI
import os
from openai import AzureOpenAI
import logging
from typing import Dict, Optional, List, Tuple, Any
from google import genai
from google.genai import types
from google.genai.types import (
    HarmCategory,
    HarmBlockThreshold,
    SafetySetting,
    FunctionResponse,
    FunctionCall,
)
import config
import base64
import json
from collections import deque
import time
import process_user
import asyncio
from concurrent.futures import ThreadPoolExecutor

from ai_tools import (
    GEMINI_FUNCTION_DECLARATIONS,
    GEMINI_TOOL_HANDLERS,
    set_tool_request_context,
    clear_tool_request_context,
)

# 创建线程池执行器用于异步调用阻塞式API
executor = ThreadPoolExecutor(max_workers=10)

ToolLog = Dict[str, Any]
AIResponse = Tuple[str, List[ToolLog]]


def _convert_gemini_tools_to_openai_format() -> list:
    """将 Gemini 工具定义转换为 OpenAI/ZhipuAI 格式"""
    tools = []
    for func_decl in GEMINI_FUNCTION_DECLARATIONS:
        tools.append({
            "type": "function",
            "function": {
                "name": func_decl.name,
                "description": func_decl.description,
                "parameters": func_decl.parameters
            }
        })
    return tools

SYSTEM_PROMPT = config.SYSTEM_PROMPT
AI_SERVICE_ORDER = config.AI_SERVICE_ORDER
ZhipuAI_API_KEY = config.ZhipuAI_API_KEY

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


# def add_prompt_user_extra_info(user_id: int):
#     """为用户添加系统提示和额外信息"""
#     user_coins = process_user.get_user_coins(user_id)
#     user_permissions = process_user.get_user_permission(user_id)
#     return "\n## 状态信息 - 用户硬币数量: {user_coins}，权限等级: {user_permissions}。".format(user_coins=user_coins, user_permissions=user_permissions)


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
    client = ZhipuAI(api_key=ZhipuAI_API_KEY)
    response = client.chat.completions.create(
        model="glm-4.5-flash",
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
    """调用ZhipuAI对图像进行分析并返回描述文本（异步版本）"""
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
        logging.error(f"连接ZhipuAI服务失败: {str(e)}")
        return "Failed to connect to AI service."
        
    except Exception as e:
        logging.error(f"处理图片时发生未知错误: {str(e)}")
        return "An error occurred while processing the image."


def _sync_analyze_image(base64_str):
    """同步版本的图像分析函数，供异步函数调用"""
    client = ZhipuAI(api_key=ZhipuAI_API_KEY)
    response = client.chat.completions.create(
        model="GLM-4.1V-Thinking-Flash",
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
    """同步版本的智谱AI响应函数（支持工具调用）"""
    client = ZhipuAI(api_key=ZhipuAI_API_KEY)

    # 获取 OpenAI 格式的工具定义
    tools = _convert_gemini_tools_to_openai_format()
    
    # 添加 ZhipuAI 特有的网络工具
    tools.extend([{
        "type": "web_search",
        "web_search": {
            "enable": True  # 启用网络搜索
        }
    }, {
        "type": "web_browser",
        "web_browser": {
            "browser": "auto"
        }
    }])

    # 添加系统消息
    system_message = {
        "role": "system",
        "content": _compose_system_prompt(tool_context),
    }
    
    # 过滤掉 content 为 null 的消息
    filtered_messages = [msg for msg in messages if msg.get("content") is not None]
    filtered_messages.insert(0, system_message)

    last_tool_payload = None
    tool_logs: List[ToolLog] = []

    # 工具调用循环（最多10轮）
    for iteration in range(10):
        response = client.chat.completions.create(
            model="glm-4.5-flash",
            messages=filtered_messages,
            tools=tools,
            tool_choice="auto",
            temperature=1.0
        )
        
        assistant_message = response.choices[0].message
        tool_calls = getattr(assistant_message, 'tool_calls', None)
        assistant_content = assistant_message.content or ""
        
        # 如果没有工具调用，直接返回当前模型答案
        if not tool_calls:
            logging.info(f"ZhipuAI 第 {iteration + 1} 轮：无工具调用，直接返回答案")
            content_text = assistant_content
            if content_text.strip():
                return content_text, tool_logs
            if last_tool_payload:
                fallback = _format_tool_fallback(last_tool_payload) or ""
                if fallback:
                    return fallback, tool_logs
            logging.warning("ZhipuAI 返回内容为空且无可用回退。")
            return content_text, tool_logs
        
        logging.info(f"ZhipuAI 第 {iteration + 1} 轮：检测到 {len(tool_calls)} 个工具调用")
        
        # 追加助手消息（包含 tool_calls）
        filtered_messages.append({
            "role": "assistant",
            "content": assistant_content,
            "tool_calls": tool_calls
        })
        
        # 执行所有工具调用
        for tool_call in tool_calls:
            function_name = tool_call.function.name
            
            # 跳过 ZhipuAI 内置工具（web_search, web_browser）
            if function_name in ["web_search", "web_browser"]:
                continue
            
            try:
                raw_args = tool_call.function.arguments or "{}"
                function_args = json.loads(raw_args)
            except json.JSONDecodeError as e:
                logging.error(f"ZhipuAI 工具参数解析失败: {e}")
                function_args = {}
            
            # 执行工具
            handler = GEMINI_TOOL_HANDLERS.get(function_name)
            if handler:
                try:
                    tool_result = handler(**function_args)
                    logging.info(
                        f"ZhipuAI 工具执行成功: {function_name}, "
                        f"args={json.dumps(function_args, ensure_ascii=False)}"
                    )
                except TypeError as e:
                    logging.error(f"ZhipuAI 工具参数错误: {function_name}, {e}")
                    tool_result = {"error": f"参数错误: {str(e)}"}
                except Exception as e:
                    logging.exception(f"ZhipuAI 工具执行失败: {function_name}, {e}")
                    tool_result = {"error": f"执行失败: {str(e)}"}
            else:
                logging.warning(f"ZhipuAI 未知工具: {function_name}")
                tool_result = {"error": f"未知工具: {function_name}"}
            
            # 追加工具返回结果
            filtered_messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": json.dumps(tool_result, ensure_ascii=False)
            })
            last_tool_payload = (function_name, tool_result)
            tool_logs.append({
                "tool_name": function_name,
                "arguments": function_args,
                "result": tool_result,
            })

    logging.warning("ZhipuAI 工具调用次数超限（10轮）")
    return "抱歉，处理您的请求时遇到了问题，请稍后再试。", tool_logs


def get_ai_response_azure(messages, user_id: int, tool_context: Optional[Dict[str, object]] = None) -> AIResponse:
    """同步版本的Azure OpenAI响应函数（支持工具调用）"""
    client = AzureOpenAI(
        azure_endpoint=config.AZURE_OPENAI_API_ENDPOINT,
        api_key=config.AZURE_OPENAI_API_KEY,
        api_version=config.AZURE_OPENAI_API_VERSION,
    )

    # 获取 OpenAI 格式的工具定义
    tools = _convert_gemini_tools_to_openai_format()

    # 添加系统消息
    system_message = {
        "role": "system",
        "content": _compose_system_prompt(tool_context)
    }
    
    # 过滤掉 content 为 null 的消息
    filtered_messages = [msg for msg in messages if msg.get("content") is not None]
    filtered_messages.insert(0, system_message)

    last_tool_payload = None
    tool_logs: List[ToolLog] = []

    try:
        # 工具调用循环（最多10轮）
        for iteration in range(10):
            completion = client.chat.completions.create(
                model="gpt-4",
                messages=filtered_messages,
                tools=tools,
                tool_choice="auto",
                temperature=1.0
            )
            
            assistant_message = completion.choices[0].message
            tool_calls = getattr(assistant_message, 'tool_calls', None)
            assistant_content = assistant_message.content or ""
            
            # 如果没有工具调用，直接返回当前模型答案
            if not tool_calls:
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
            
            logging.info(f"Azure 第 {iteration + 1} 轮：检测到 {len(tool_calls)} 个工具调用")
            
            # 追加助手消息（包含 tool_calls）
            filtered_messages.append({
                "role": "assistant",
                "content": assistant_content,
                "tool_calls": tool_calls
            })
            
            # 执行所有工具调用
            for tool_call in tool_calls:
                function_name = tool_call.function.name
                
                try:
                    raw_args = tool_call.function.arguments or "{}"
                    function_args = json.loads(raw_args)
                except json.JSONDecodeError as e:
                    logging.error(f"Azure 工具参数解析失败: {e}")
                    function_args = {}
                
                # 执行工具
                handler = GEMINI_TOOL_HANDLERS.get(function_name)
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
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(tool_result, ensure_ascii=False)
                })
                last_tool_payload = (function_name, tool_result)
                tool_logs.append({
                    "tool_name": function_name,
                    "arguments": function_args,
                    "result": tool_result,
                })
        
        logging.warning("Azure 工具调用次数超限（10轮）")
        return "抱歉，处理您的请求时遇到了问题，请稍后再试。", tool_logs
        
    except Exception as e:
        logging.error(f"Azure OpenAI 请求失败: {e}")
        raise


def get_ai_response_google(messages, user_id: int, tool_context: Optional[Dict[str, object]] = None) -> AIResponse:
    """同步版本的Google Gemini响应函数（使用最新生成接口并支持工具调用）。"""
    client = genai.Client(api_key=config.GEMINI_API_KEY)

    system_prompt = _compose_system_prompt(tool_context)

    safety_settings = [
        SafetySetting(
            category=HarmCategory.HARM_CATEGORY_HATE_SPEECH,
            threshold=HarmBlockThreshold.BLOCK_NONE
        ),
        SafetySetting(
            category=HarmCategory.HARM_CATEGORY_HARASSMENT,
            threshold=HarmBlockThreshold.BLOCK_NONE
        ),
        SafetySetting(
            category=HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
            threshold=HarmBlockThreshold.BLOCK_NONE
        ),
        SafetySetting(
            category=HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
            threshold=HarmBlockThreshold.BLOCK_NONE
        ),
        SafetySetting(
            category=HarmCategory.HARM_CATEGORY_CIVIC_INTEGRITY,
            threshold=HarmBlockThreshold.BLOCK_NONE
        )
    ]

    contents = _build_gemini_contents(messages)

    generation_config_kwargs = dict(
        system_instruction=system_prompt,
        tools=[
            types.Tool(function_declarations=GEMINI_FUNCTION_DECLARATIONS),
        ],
        tool_config=types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(mode="AUTO")
        ),
        safety_settings=safety_settings,
    )

    try:
        return _generate_gemini_response(
            client=client,
            model_name="gemini-flash-latest",
            contents=contents,
            generation_config_kwargs=generation_config_kwargs,
            temperature=1.0,
        )
    except Exception as e:
        error_str = str(e)
        if 'RESOURCE_EXHAUSTED' in error_str:
            logging.warning(f"gemini-flash资源耗尽错误，尝试使用 gemini-flash-lite 模型重试: {error_str}")
            try:
                return _generate_gemini_response(
                    client=client,
                    model_name="gemini-flash-lite-latest",
                    contents=contents,
                    generation_config_kwargs=generation_config_kwargs,
                    temperature=1.0,
                )
            except Exception as retry_e:
                logging.error(f"使用 gemini-flash-lite 重试失败: {str(retry_e)}")
                raise retry_e
        if 'SAFETY' in error_str and 'blocked' in error_str:
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


def _build_gemini_contents(messages):
    """
    将内部消息格式转换为 Gemini 所需的 Contents 列表。
    """
    contents = []
    for message in messages:
        role = message.get("role")
        content = message.get("content")
        if content is None or role == "system":
            continue

        if not isinstance(content, str):
            logging.warning("Unsupported Gemini message content type: %s", type(content))
            continue

        gemini_role = "user" if role == "user" else "model"
        contents.append(
            types.Content(
                role=gemini_role,
                parts=[types.Part(text=content)]
            )
        )
    return contents


def _generate_gemini_response(
    client,
    model_name,
    contents,
    generation_config_kwargs,
    temperature: float,
) -> AIResponse:
    """
    调用 Gemini models.generate_content，自动处理函数调用并返回最终文本。
    """
    conversation = list(contents)
    last_tool_payload = None
    tool_logs: List[ToolLog] = []

    generate_config = types.GenerateContentConfig(
        **generation_config_kwargs,
        temperature=temperature,
    )

    while True:
        response = client.models.generate_content(
            model=model_name,
            contents=conversation,
            config=generate_config,
        )

        if getattr(response, "function_calls", None):
            logging.info(
                "Gemini requested function call(s): %s",
                [fc.name for fc in response.function_calls],
            )

        function_call = _extract_function_call(response)
        if not function_call:
            final_text = response.text or ""
            if final_text.strip():
                return final_text, tool_logs
            if last_tool_payload:
                summary_text = _format_tool_fallback(last_tool_payload)
                if summary_text:
                    logging.info("Gemini text empty after tool call; using fallback summary.")
                    return summary_text, tool_logs
                logging.warning("Gemini text empty after tool call and no fallback available.")
            return final_text, tool_logs

        tool_name = getattr(function_call, "name", None)
        handler = GEMINI_TOOL_HANDLERS.get(tool_name)

        if handler is None:
            logging.warning("Unknown Gemini tool call received: %s", tool_name)
            return response.text or "", tool_logs

        raw_args = getattr(function_call, "args", {}) or {}
        if not isinstance(raw_args, dict):
            logging.warning("Gemini tool args format invalid: %s", raw_args)
            raw_args = {}
        call_args = dict(raw_args)

        try:
            tool_result = handler(**call_args)
        except TypeError as exc:
            logging.error("Gemini tool parameter error: %s", exc)
            tool_result = {"error": f"参数错误: {str(exc)}"}
        except Exception as exc:
            logging.exception("Gemini tool execution failed: %s", exc)
            tool_result = {"error": "工具执行失败"}

        logging.info(
            "Gemini tool executed: %s args=%s result=%s",
            tool_name,
            json.dumps(call_args, ensure_ascii=True),
            json.dumps(tool_result, ensure_ascii=True, default=str),
        )
        last_tool_payload = (tool_name, tool_result)

        fc_kwargs = {
            "name": getattr(function_call, "name", None),
            "args": call_args,
        }
        function_id = getattr(function_call, "id", None)
        if function_id is not None:
            fc_kwargs["id"] = function_id

        conversation.append(
            types.Content(
                role="model",
                parts=[types.Part(function_call=FunctionCall(**fc_kwargs))]
            )
        )

        fr_kwargs = {
            "name": tool_name,
            "response": tool_result,
        }
        if function_id is not None:
            fr_kwargs["id"] = function_id

        conversation.append(
            types.Content(
                role="tool",
                parts=[
                    types.Part(
                        function_response=FunctionResponse(**fr_kwargs)
                    )
                ],
            )
        )
        tool_logs.append({
            "tool_name": tool_name,
            "arguments": call_args,
            "result": tool_result,
        })


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


def _extract_function_call(response):
    """
    Extract the first function call from a Gemini response, if any.
    """
    function_calls = getattr(response, "function_calls", None)
    if function_calls:
        return function_calls[0]

    candidates = getattr(response, "candidates", []) or []
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        if not content:
            continue
        parts = getattr(content, "parts", []) or []
        for part in parts:
            function_call = getattr(part, "function_call", None)
            if function_call:
                return function_call
    return None
