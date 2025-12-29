import asyncio
import logging
import time
from collections import deque

from core import config

from ..clients import create_zhipu_client
from ..runtime import EXECUTOR


class APIRateLimiter:
    def __init__(self, max_requests: int = 10, time_window: int = 60) -> None:
        self.requests = deque()
        self.max_requests = max_requests
        self.time_window = time_window

    def can_make_request(self) -> bool:
        now = time.time()
        while self.requests and now - self.requests[0] > self.time_window:
            self.requests.popleft()

        if len(self.requests) < self.max_requests:
            self.requests.append(now)
            return True
        return False


translate_limiter = APIRateLimiter(max_requests=10, time_window=60)


async def translate_text(text: str) -> str:
    """专门用于文本翻译的AI函数（异步版本）"""
    try:
        if not translate_limiter.can_make_request():
            return "请求过于频繁，请稍后再试。\nToo many requests, please try again later."

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            EXECUTOR,
            lambda: _sync_translate_text(text),
        )
    except Exception as exc:
        logging.error("翻译过程中出错: %s", exc)
        return "翻译失败，请稍后重试。\nTranslation failed, please try again later."


def _sync_translate_text(text: str) -> str:
    """同步版本的翻译函数，供异步函数调用"""
    client = create_zhipu_client()
    response = client.chat.completions.create(
        model=config.ZHIPU_TRANSLATE_MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a professional translation assistant. If the user enters Chinese, "
                    "please translate it into English; if the user enters English, please translate "
                    "it into Chinese. The translation should be colloquial, cat-girl like, cute, "
                    "and adorable."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Only output the final translated text, please translate the following text: "
                    f"\n{text}"
                ),
            },
        ],
    )
    return response.choices[0].message.content

