"""群聊里判断一条消息是否该唤起 AI。

直接触发词与 @提及走本地判断；classifier 走一次轻量模型调用，并由限流器
保护，避免群聊高峰把额度打满。
"""

import asyncio
import logging
import time
from collections import deque

from core import config
from features.ai.task_runner import run_ai_task

from . import lifecycle


class RateLimiter:
    def __init__(self, max_calls: int, time_window: float):
        self.max_calls = max_calls
        self.time_window = time_window
        self.calls = deque()

    def consume(self) -> bool:
        now = time.time()
        while self.calls and now - self.calls[0] > self.time_window:
            self.calls.popleft()
        if len(self.calls) < self.max_calls:
            self.calls.append(now)
            return True
        return False


_classifier_allowance = RateLimiter(max_calls=10, time_window=60.0)


async def should_trigger_ai_response(message_text: str) -> bool:
    """
    使用配置的 classifier AI 模型判断群聊消息是否需要调用主 AI 回复。
    仅返回布尔结果，出现异常时默认不触发回复。
    """
    if not message_text:
        return False

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: _sync_should_trigger_ai_response(message_text)
    )


def _sync_should_trigger_ai_response(message_text: str) -> bool:
    if not _classifier_allowance.consume():
        logging.debug("AI classifier rate limiter blocked a request.")
        return False
    try:
        response = run_ai_task(
            "classifier",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是一个简洁的分类器。判断给定消息是否需要雾萌娘机器人主动回复。"
                        "仅在遇到相关问题必要时才回复，例如和AI聊天、寻求帮助、提问或请求信息等。"
                        "如果需要回复，请只回答 YES；如果不需要，请只回答 NO。"
                        "不要输出任何额外解释。"
                    ),
                },
                {
                    "role": "user",
                    "content": message_text,
                },
            ],
        )
        content = response.choices[0].message.content.strip().lower()
        return content.startswith("yes") or content.startswith("是")
    except Exception as exc:
        logging.error("AI 检测是否应回复失败: %s", exc)
        return False


def _message_trigger_text(message) -> str:
    parts = []
    text = getattr(message, "text", None)
    caption = getattr(message, "caption", None)
    if text:
        parts.append(str(text))
    if caption:
        parts.append(str(caption))
    return "\n".join(parts)


def _direct_trigger_phrases() -> list[str]:
    trigger_phrases = [
        str(trigger).strip().lower()
        for trigger in config.AI_DIRECT_TRIGGER_PHRASES
        if str(trigger).strip()
    ]
    bot_username = (lifecycle._BOT_USERNAME or "FogMoeBot").strip().lower()
    if bot_username:
        trigger_phrases.append(f"@{bot_username}")
    return trigger_phrases


def message_contains_direct_ai_trigger(message) -> bool:
    message_text = _message_trigger_text(message)
    if not message_text:
        return False

    normalized_text = message_text.lower()
    return any(trigger in normalized_text for trigger in _direct_trigger_phrases())
