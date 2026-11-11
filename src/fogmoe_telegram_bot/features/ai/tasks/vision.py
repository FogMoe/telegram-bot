"""调用 vision 任务生成图片或贴纸的文字描述，用于写入纯文本会话历史。"""

from __future__ import annotations

import logging

from ..task_runner import run_ai_task


async def analyze_image(base64_str: str) -> str | None:
    """调用配置的 AI vision 模型描述图片。

    识别失败或描述为空时返回 None，调用方不往历史里写描述：当轮的多模态主模型照样看得到原图，
    历史里留一句「识别失败」反而会和它当时的回复对不上。
    """
    if not base64_str:
        logging.error("图片数据为空，跳过识别")
        return None
    try:
        description = await _describe_image(base64_str)
    except Exception as exc:
        logging.error("处理图片描述时失败: %s", exc)
        return None
    return description.strip() or None


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
