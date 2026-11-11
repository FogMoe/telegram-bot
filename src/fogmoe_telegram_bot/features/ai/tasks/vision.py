"""调用 vision 任务生成图片或贴纸的文字描述，用于写入纯文本会话历史。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

from ..task_runner import run_ai_task


class VisionStatus(StrEnum):
    OK = "ok"
    EMPTY = "empty"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class VisionResult:
    """图片识别结果：失败时返回结构化状态而非英文错误文案，避免把错误句子写进聊天历史。"""

    status: VisionStatus
    description: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is VisionStatus.OK and bool(
            self.description and self.description.strip()
        )


async def analyze_image(base64_str: str) -> VisionResult:
    """调用配置的 AI vision 模型分析图像并返回类型化的 `VisionResult`。"""
    if not base64_str or not base64_str.strip():
        logging.error("图片数据验证失败: Image data is empty.")
        return VisionResult(status=VisionStatus.EMPTY, error="empty_image_data")

    try:
        description = await _describe_image(base64_str)
    except Exception as exc:
        logging.error("处理图片描述时失败: %s", exc)
        return VisionResult(status=VisionStatus.FAILED, error=type(exc).__name__)

    cleaned = (description or "").strip()
    if not cleaned:
        logging.warning("AI vision 返回了空的图片描述")
        return VisionResult(status=VisionStatus.FAILED, error="empty_vision_response")
    return VisionResult(status=VisionStatus.OK, description=cleaned)


async def _describe_image(base64_str: str) -> str:
    response = await run_ai_task(
        "vision",
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": base64_str},
                    },
                    {
                        "type": "text",
                        "text": (
                            "Please provide a detailed description of this image, including main "
                            "objects, scene, actions, colors, atmosphere and other key elements. "
                            "If it's an emoji or sticker, please explain its emotional expression "
                            "and meaning. Use clear and concise language in your description."
                        ),
                    },
                ],
            }
        ],
    )
    choices = getattr(response, "choices", None) or []
    if not choices:
        raise RuntimeError("Vision provider returned a response without choices")
    content = getattr(choices[0].message, "content", None)
    return str(content or "")
