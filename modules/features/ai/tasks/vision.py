import asyncio
import logging

from ..runtime import EXECUTOR
from ..task_runner import run_ai_task


async def analyze_image(base64_str):
    """调用配置的 AI vision 模型分析图像并返回描述文本（异步版本）"""
    try:
        if not base64_str:
            raise ValueError("Image data is empty.")

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            EXECUTOR,
            lambda: _sync_analyze_image(base64_str),
        )

    except ValueError as exc:
        logging.error("图片数据验证失败: %s", exc)
        return "Image validation failed, please check the image format."

    except ConnectionError as exc:
        logging.error("连接 AI vision 服务失败: %s", exc)
        return "Failed to connect to AI service."

    except Exception as exc:
        logging.error("处理图片时发生未知错误: %s", exc)
        return "An error occurred while processing the image."


def _sync_analyze_image(base64_str):
    """同步版本的图像分析函数，供异步函数调用"""
    response = run_ai_task(
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
    return response.choices[0].message.content

