import logging
import threading
from typing import Any, Awaitable, Callable

from .generated_audio_sender import send_generated_audio_from_tool_result
from .generated_image_sender import send_generated_images_from_tool_result
from .reply_filter import normalize_ai_reply_text
from .sticker_sender import (
    PartialAIReplySendError,
    normalize_sticker_directives,
    send_ai_reply_with_stickers,
)
from .types import JobAbortedError

AsyncSendFunc = Callable[..., Awaitable[Any]]


class TelegramVisibleContentHandler:
    """工具循环向用户即时发送可见内容的处理器：在事件循环里直接 `await`，没有跨线程投递。

    调用顺序与 E 的撤销语义不变：每次发送前检查 `abort_event`，已置位就抛 `JobAbortedError`，
    之后不再向用户发送任何内容（定时任务与空闲跟进失去 claim 时靠它阻止后续投递）。
    """

    def __init__(
        self,
        *,
        bot: Any,
        chat_id: int,
        first_text_send: AsyncSendFunc,
        fallback_send: AsyncSendFunc,
        logger: logging.Logger,
        reply_to_message_id: int | None = None,
        abort_event: threading.Event | None = None,
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.first_text_send = first_text_send
        self.fallback_send = fallback_send
        self.logger = logger
        self.reply_to_message_id = reply_to_message_id
        # 后台任务失去 claim 后置位：此后不再向用户发送任何内容。
        self.abort_event = abort_event
        self.sent_messages: list[Any] = []
        self.sent_contents: list[str] = []
        self.sent_count = 0
        self.attempted_count = 0

    async def _send(self, content: str) -> str:
        reply_text = normalize_ai_reply_text(content)
        if not reply_text.strip():
            return ""

        normalized = await normalize_sticker_directives(
            reply_text,
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
        # 贴纸校验与输入状态都是 await：租约可能在这期间丢失，真正发送之前再检查一次。
        self._raise_if_aborted()
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

    def _raise_if_aborted(self) -> None:
        if self.abort_event is not None and self.abort_event.is_set():
            raise JobAbortedError("visible content send aborted")

    async def __call__(self, content: str) -> str | None:
        self._raise_if_aborted()
        return await self._send(content)

    async def _send_tool_media(self, tool_name: str, result: dict[str, Any]) -> list[Any]:
        action = "upload_photo" if tool_name == "generate_image" else "upload_voice"
        try:
            await self.bot.send_chat_action(chat_id=self.chat_id, action=action)
        except Exception:
            self.logger.debug("Failed to send upload action before generated media")
        self._raise_if_aborted()

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

    async def send_tool_media(self, tool_name: str, result: dict[str, Any]) -> list[Any]:
        self._raise_if_aborted()
        return await self._send_tool_media(tool_name, result)

    def visible_events(self) -> list[dict[str, str]]:
        return [
            {
                "type": "assistant_visible",
                "content": content,
            }
            for content in self.sent_contents
        ]
