import asyncio
import logging

from core import config

from ..clients import create_zhipu_client
from ..runtime import EXECUTOR


async def analyze_image(base64_str):
    """调用 Z.ai 对图像进行分析并返回描述文本（异步版本）"""
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
        logging.error("连接 Z.ai 服务失败: %s", exc)
        return "Failed to connect to AI service."

    except Exception as exc:
        logging.error("处理图片时发生未知错误: %s", exc)
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

