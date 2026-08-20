"""AI 出站发送的共用管线。

对话、定时任务和空闲跟进都要把同一轮工具产出的媒体发出去，这里统一实现，
避免三处各写一套抑制历史记录的作用域。
"""

from core.telegram_history import suppress_telegram_history

from .generated_audio_sender import send_generated_audio_from_tool_logs
from .generated_image_sender import send_generated_images_from_tool_logs


async def send_generated_media(*, bot, chat_id, tool_logs, logger) -> list:
    """发送本轮工具产出的语音与图片，返回已发出的消息。

    这些消息由工具产生而非用户可见对话，因此写入期间抑制历史记录。
    """

    sent_messages = []
    with suppress_telegram_history():
        sent_messages.extend(
            await send_generated_audio_from_tool_logs(
                bot=bot,
                chat_id=chat_id,
                tool_logs=tool_logs,
                logger=logger,
            )
        )
        sent_messages.extend(
            await send_generated_images_from_tool_logs(
                bot=bot,
                chat_id=chat_id,
                tool_logs=tool_logs,
                logger=logger,
            )
        )
    return sent_messages
