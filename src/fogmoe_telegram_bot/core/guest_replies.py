"""Guest Mode（Bot API 10.0）的回复通道：把发往召唤聊天的请求改写成同一条 guest 回复。

bot 不在召唤它的聊天里，`sendMessage` 发不进去：只能用 `answerGuestQuery` 回一条 inline 消息，
之后凭 `inline_message_id` 编辑它。在 `guest_reply_scope` 里，发往召唤聊天的请求在 `_do_post` 被改写：

- 第一次 `sendMessage` → `answerGuestQuery`；之后每次 `sendMessage` 在这条消息里追加一段，
  `editMessageText`、`editMessageReplyMarkup`、`deleteMessage` 改或删对应的段，然后整条重绘；
- 返回合成的 `Message`（message_id 是段的 id），handler 照常拿它编辑或删除；
- `sendChatAction` 直接成功；其余发往这个聊天的方法抛 `GuestModeUnsupported`。

发往别的聊天（例如私聊调用者）的请求照常发出。拦在 `_do_post` 是因为所有 Bot 方法都经过它，
且这时默认值已经填好；PTB 的 `ExtBot` 也在这一层做限流。

Telegram 文档没写 guest 回复能否凭 `inline_message_id` 编辑，测试里的编辑是替身，还没有真机验证。
目前的自我介绍只发一条、不编辑；把会发多条或会编辑的功能接进来之前，先在真机上召唤一次并编辑确认。
"""

from __future__ import annotations

import asyncio
import itertools
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Any

from telegram import (
    Chat,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    LinkPreviewOptions,
    Message,
    MessageEntity,
)
from telegram.error import BadRequest
from telegram.ext import ExtBot

MAX_TEXT_UTF16 = 4096
SEGMENT_SEPARATOR = "\n\n"
# 段全部删掉之后 inline 消息删不掉，只能改成这个。
EMPTY_TEXT = "…"
_RESULT_ID = "guest-reply"
_RESULT_TITLE = "FogMoeBot"

# 合成的 message_id 从十亿起，避开真实消息的 id：handler 删除召唤消息时不会删到回复里的段。
_SYNTHETIC_MESSAGE_IDS = itertools.count(1_000_000_000)


class GuestModeUnsupported(BadRequest):
    """guest 回复做不到的请求（发图、封禁、读成员……），按 Telegram 拒绝了请求处理。"""


@dataclass(frozen=True)
class _Segment:
    message_id: int
    text: str
    parse_mode: str | None = None
    entities: tuple[MessageEntity, ...] = ()
    link_preview_options: LinkPreviewOptions | None = None
    reply_markup: InlineKeyboardMarkup | None = None


@dataclass
class GuestReplySession:
    """一次召唤的回复状态：guest query、召唤所在的聊天、已发出的 inline 消息与它的各段。"""

    query_id: str
    chat: Chat
    inline_message_id: str | None = None
    segments: list[_Segment] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @classmethod
    def from_message(cls, message: Message) -> GuestReplySession:
        if not message.guest_query_id:
            raise ValueError("message has no guest_query_id")
        return cls(query_id=message.guest_query_id, chat=message.chat)

    def targets(self, data: dict[str, Any]) -> bool:
        chat_id = data.get("chat_id")
        return chat_id is not None and str(chat_id) == str(self.chat.id)

    def index_of(self, message_id: Any) -> int:
        for index, segment in enumerate(self.segments):
            if segment.message_id == message_id:
                return index
        raise GuestModeUnsupported(f"Message {message_id} is not part of the guest reply")


_SESSION: ContextVar[GuestReplySession | None] = ContextVar("guest_reply_session", default=None)


@contextmanager
def guest_reply_scope(session: GuestReplySession) -> Iterator[GuestReplySession]:
    token = _SESSION.set(session)
    try:
        yield session
    finally:
        _SESSION.reset(token)


def current_guest_session() -> GuestReplySession | None:
    return _SESSION.get()


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _shift(entity: MessageEntity, offset: int) -> MessageEntity:
    return MessageEntity.de_json({**entity.to_dict(), "offset": entity.offset + offset}, None)


def _render(segments: Sequence[_Segment]) -> tuple[str, str | None, tuple[MessageEntity, ...]]:
    """把各段拼成一条消息的 (text, parse_mode, entities)。

    各段 parse_mode 相同时原样拼接；不同的格式拼不到一起，只显示最新一段。纯文本加 entities 时
    超长就从最早的段开始丢，至少保留最新一段（它本身超长由 Telegram 拒绝）。
    """
    if not segments:
        return EMPTY_TEXT, None, ()
    parse_modes = {segment.parse_mode for segment in segments}
    if len(parse_modes) > 1:
        latest = segments[-1]
        return latest.text, latest.parse_mode, latest.entities
    parse_mode = segments[0].parse_mode
    if parse_mode is not None:
        return SEGMENT_SEPARATOR.join(segment.text for segment in segments), parse_mode, ()

    separator_length = _utf16_len(SEGMENT_SEPARATOR)
    kept: list[_Segment] = []
    length = 0
    for segment in reversed(segments):
        added = _utf16_len(segment.text) + (separator_length if kept else 0)
        if kept and length + added > MAX_TEXT_UTF16:
            break
        kept.append(segment)
        length += added
    kept.reverse()

    parts: list[str] = []
    entities: list[MessageEntity] = []
    offset = 0
    for segment in kept:
        if parts:
            parts.append(SEGMENT_SEPARATOR)
            offset += separator_length
        entities.extend(_shift(entity, offset) for entity in segment.entities)
        parts.append(segment.text)
        offset += _utf16_len(segment.text)
    return "".join(parts), None, tuple(entities)


def _inline_markup(data: dict[str, Any]) -> InlineKeyboardMarkup | None:
    markup = data.get("reply_markup")
    return markup if isinstance(markup, InlineKeyboardMarkup) else None


def _text_fields(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "text": str(data.get("text") or ""),
        "parse_mode": data.get("parse_mode"),
        "entities": tuple(data.get("entities") or ()),
        "link_preview_options": data.get("link_preview_options"),
        "reply_markup": _inline_markup(data),
    }


class GuestReplyExtBot(ExtBot):
    """在 `guest_reply_scope` 里把发往召唤聊天的请求改写成 guest 回复，见模块说明。"""

    async def _do_post(self, endpoint: str, data: dict[str, Any], **kwargs: Any) -> Any:
        session = _SESSION.get()
        if session is None or not session.targets(data):
            return await super()._do_post(endpoint, data, **kwargs)
        async with session.lock:
            return await self._guest_request(session, endpoint, data)

    async def _guest_request(
        self,
        session: GuestReplySession,
        endpoint: str,
        data: dict[str, Any],
    ) -> Any:
        if endpoint == "sendChatAction":
            return True
        if endpoint == "sendMessage":
            segment = _Segment(next(_SYNTHETIC_MESSAGE_IDS), **_text_fields(data))
            await self._redraw(session, [*session.segments, segment])
            return self._message_data(session, segment)
        if endpoint in ("editMessageText", "editMessageReplyMarkup"):
            index = session.index_of(data.get("message_id"))
            current = session.segments[index]
            if endpoint == "editMessageText":
                edited = replace(current, **_text_fields(data))
            else:
                edited = replace(current, reply_markup=_inline_markup(data))
            segments = list(session.segments)
            segments[index] = edited
            await self._redraw(session, segments)
            return self._message_data(session, edited)
        if endpoint == "deleteMessage":
            index = session.index_of(data.get("message_id"))
            await self._redraw(session, session.segments[:index] + session.segments[index + 1 :])
            return True
        raise GuestModeUnsupported(f"{endpoint} is not available in a guest reply")

    async def _redraw(self, session: GuestReplySession, segments: list[_Segment]) -> None:
        """把 inline 消息改成 `segments` 的样子；成功之后才更新会话，失败时会话保持原样。"""
        text, parse_mode, entities = _render(segments)
        reply_markup = next(
            (segment.reply_markup for segment in reversed(segments) if segment.reply_markup),
            None,
        )
        link_preview_options = segments[-1].link_preview_options if segments else None
        if session.inline_message_id is None:
            sent = await super().answer_guest_query(
                session.query_id,
                InlineQueryResultArticle(
                    id=_RESULT_ID,
                    title=_RESULT_TITLE,
                    input_message_content=InputTextMessageContent(
                        text,
                        parse_mode=parse_mode,
                        entities=entities or None,
                        link_preview_options=link_preview_options,
                    ),
                    reply_markup=reply_markup,
                ),
            )
            session.inline_message_id = sent.inline_message_id
        else:
            try:
                await super().edit_message_text(
                    text,
                    inline_message_id=session.inline_message_id,
                    parse_mode=parse_mode,
                    entities=entities or None,
                    link_preview_options=link_preview_options,
                    reply_markup=reply_markup,
                )
            except BadRequest as exc:
                if "not modified" not in exc.message.lower():
                    raise
        session.segments = segments

    def _message_data(self, session: GuestReplySession, segment: _Segment) -> dict[str, Any]:
        """handler 拿到的合成消息。parse_mode 的段原样返回带标记的 text，不在本地解析。"""
        data: dict[str, Any] = {
            "message_id": segment.message_id,
            "date": int(time.time()),
            "chat": session.chat.to_dict(),
            "text": segment.text,
        }
        if segment.entities:
            data["entities"] = [entity.to_dict() for entity in segment.entities]
        if segment.reply_markup is not None:
            data["reply_markup"] = segment.reply_markup.to_dict()
        try:
            data["from"] = self.bot.to_dict()
        except RuntimeError:
            pass
        return data
