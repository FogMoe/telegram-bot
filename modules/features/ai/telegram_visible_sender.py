import asyncio
import logging
from typing import Any, Awaitable, Callable

from .generated_audio_sender import send_generated_audio_from_tool_result
from .generated_image_sender import send_generated_images_from_tool_result
from .sticker_sender import (
    PartialAIReplySendError,
    normalize_sticker_directives,
    send_ai_reply_with_stickers,
)

AsyncSendFunc = Callable[..., Awaitable[Any]]


class TelegramVisibleContentHandler:
    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        bot: Any,
        chat_id: int,
        first_text_send: AsyncSendFunc,
        fallback_send: AsyncSendFunc,
        logger: logging.Logger,
        reply_to_message_id: int | None = None,
    ) -> None:
        self.loop = loop
        self.bot = bot
        self.chat_id = chat_id
        self.first_text_send = first_text_send
        self.fallback_send = fallback_send
        self.logger = logger
        self.reply_to_message_id = reply_to_message_id
        self.sent_messages: list[Any] = []
        self.sent_contents: list[str] = []
        self.sent_count = 0
        self.attempted_count = 0

    async def _send(self, content: str) -> str:
        normalized = await normalize_sticker_directives(
            str(content),
            logger=self.logger,
        )
        if not normalized.strip():
            return ""

        use_first_send = self.sent_count == 0
        self.attempted_count += 1
        try:
            await self.bot.send_chat_action(chat_id=self.chat_id, action="typing")
        except Exception:
            self.logger.debug("Failed to send typing action before visible AI content")
        try:
            send_messages = await send_ai_reply_with_stickers(
                bot=self.bot,
                chat_id=self.chat_id,
                text=normalized,
                first_text_send=self.first_text_send if use_first_send else self.fallback_send,
                fallback_send=self.fallback_send,
                logger=self.logger,
                reply_to_message_id=self.reply_to_message_id if use_first_send else None,
            )
        except PartialAIReplySendError as exc:
            self.sent_messages.extend(exc.sent_messages)
            sent_content = (exc.sent_content or normalized).strip()
            if sent_content:
                self.sent_contents.append(sent_content)
                self.sent_count += 1
            raise
        self.sent_messages.extend(send_messages)
        self.sent_contents.append(normalized)
        self.sent_count += 1
        return normalized

    def __call__(self, content: str) -> str | None:
        future = asyncio.run_coroutine_threadsafe(self._send(content), self.loop)
        return future.result()

    async def _send_tool_media(self, tool_name: str, result: dict[str, Any]) -> list[Any]:
        action = "upload_photo" if tool_name == "generate_image" else "upload_document"
        try:
            await self.bot.send_chat_action(chat_id=self.chat_id, action=action)
        except Exception:
            self.logger.debug("Failed to send upload action before generated media")

        if tool_name == "generate_image":
            sent_messages = await send_generated_images_from_tool_result(
                bot=self.bot,
                chat_id=self.chat_id,
                result=result,
                logger=self.logger,
            )
        elif tool_name == "generate_voice":
            sent_messages = await send_generated_audio_from_tool_result(
                bot=self.bot,
                chat_id=self.chat_id,
                result=result,
                logger=self.logger,
            )
        else:
            sent_messages = []

        self.sent_messages.extend(sent_messages)
        return sent_messages

    def send_tool_media(self, tool_name: str, result: dict[str, Any]) -> list[Any]:
        future = asyncio.run_coroutine_threadsafe(
            self._send_tool_media(tool_name, result),
            self.loop,
        )
        return future.result()

    def visible_events(self) -> list[dict[str, str]]:
        return [
            {
                "type": "assistant_visible",
                "content": content,
            }
            for content in self.sent_contents
        ]
