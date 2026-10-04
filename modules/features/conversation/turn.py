"""一轮 AI 对话的业务操作。

Telegram handler（`handlers.py`）负责把 `Update` 映射成 `TurnRequest`、做群聊触发与冷却判断；
这里从「一批已通过准入的消息」开始，到「回复已投递、历史已落库」结束。阶段顺序是
`turn_types.Stage`，阶段、事务所有权与计时方式见 docs/architecture.md 的「一轮对话」。

某个阶段决定结束这一轮时（用户已经收到提示）抛内部异常 `_Stop`，`TurnResult.status` 说明原因。
"""

from __future__ import annotations

import base64
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, TypeVar

from core import balance, metrics
from core.deadline import REASON_SHUTDOWN, Deadline, DeadlineExceeded
from core.prompt_utils import format_user_state_prompt
from core.redaction import redact_text
from core.telegram_history import (
    format_user_message as format_xml_message,
)
from core.telegram_history import (
    normalize_command_name,
    suppress_telegram_history,
    telegram_history_scope,
)
from core.telegram_utils import partial_send
from features.ai import ai_chat
from features.ai.reply_filter import normalize_ai_reply_text
from features.ai.router import TURN_DEADLINE_ERROR_MESSAGE, TURN_SHUTDOWN_ERROR_MESSAGE
from features.ai.tool_history import (
    tool_logs_completed_clear,
    tool_logs_to_record_entries,
)
from features.ai.types import ToolLog

from . import billing
from . import messages as message_utils
from .turn_services import TurnServices, default_services
from .turn_types import (
    ConversationSettings,
    HistoryInsert,
    ModelRequest,
    PlannedMessage,
    Stage,
    StageTimer,
    TurnRequest,
    TurnResult,
    TurnStatus,
)

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

MESSAGE_TOO_LONG_TEXT = (
    "消息过长，无法处理。请缩短消息长度！\n"
    "The message is too long to process. Please shorten the message."
)
UNREGISTERED_TEXT = (
    "请先使用 /me 命令注册个人信息后再聊天。\n"
    "Please register first using the /me command before chatting."
)
MEDIA_TOO_LARGE_TEXT = (
    "图片太大啦，请压缩后再发送。\n"
    "The image is too large. Please compress it and try again."
)
MEDIA_FAILED_TEXT = (
    "抱歉呢，雾萌娘暂时无法处理您发送的媒体，请稍后再试试看喵~\n"
    "Sorry, I'm having trouble processing your image/sticker right now. Please try again later, meow!"
)
NEAR_LIMIT_WARNING_TEXT = (
    "提醒：当前会话历史记录已接近系统容量上限。雾萌娘可能会在稍后自动压缩较早的消息以保持体验顺畅。"
)
OVERFLOW_WARNING_TEXT = (
    "提示：为了保证会话流畅，部分较早的聊天记录已被自动压缩保存。当前对话不受影响，若需要查看完整历史请告诉雾萌娘。"
)

OVERFLOW = "overflow"

# 整轮截止时间到期之后仍然可以用的宽限：写回复历史、投递最终回复（超时提示）各自最多再等这么久。
DELIVERY_GRACE_SECONDS = 30.0


def insufficient_balance_text(total_cost: int) -> str:
    return (
        f"您的硬币不足，无法与雾萌娘连接，需要{total_cost}个硬币。试试通过 /lottery 抽奖吧！\n"
        f"You don't have enough coins (need {total_cost}), I don't want to talk to you. "
        f"Try using /lottery to get some coins!"
    )


# ---------------------------------------------------------------------------
# 纯函数：不碰 Telegram、数据库和模型，单元测试直接调用
# ---------------------------------------------------------------------------


def format_impression(raw: str | None) -> str:
    """印象文本：压成单行并截到 500 字符；没有记录时是 `Not recorded`。"""
    text = (raw or "").strip()
    if not text:
        return "Not recorded"
    text = text.replace("\r", " ").replace("\n", " ")
    if len(text) > 500:
        text = text[:497] + "..."
    return text


def format_personal_info(raw: str | None) -> str:
    return (raw or "").strip()[:500]


def merge_history_warning(current: str | None, level: str | None) -> str | None:
    """本轮里多次历史写入的容量提示合并成一个：`overflow` 优先，其余保留最先出现的。"""
    if not level:
        return current
    if current == OVERFLOW:
        return current
    if level == OVERFLOW:
        return OVERFLOW
    return level if current is None else current


def history_warning_text(level: str | None) -> str | None:
    if level == "near_limit":
        return NEAR_LIMIT_WARNING_TEXT
    if level == OVERFLOW:
        return OVERFLOW_WARNING_TEXT
    return None


def _edit_stamp(message: Any) -> int | None:
    edit_date = getattr(message, "edit_date", None)
    return int(edit_date.timestamp()) if isinstance(edit_date, datetime) else None


def build_tool_context(request: TurnRequest, user_state_prompt: str) -> dict[str, object]:
    chat = request.chat
    sender = request.sender
    return {
        "is_group": chat.is_group,
        "group_id": chat.chat_id if chat.is_group else None,
        "chat_id": chat.chat_id,
        "chat_type": chat.chat_type,
        "chat_title": chat.title,
        "message_id": getattr(request.reply_target, "message_id", None),
        # 编辑过的消息会开始新的一轮；代执行命令的身份因此与编辑前的那一轮不同。
        "message_edit_stamp": _edit_stamp(request.reply_target),
        "user_id": sender.user_id,
        "username": sender.username,
        "first_name": sender.first_name,
        "language_code": sender.language_code,
        "user_state_prompt": user_state_prompt,
    }


class _Stop(Exception):
    """某个阶段决定结束这一轮（用户已经收到提示）。仅在本模块内部使用。"""

    def __init__(self, status: TurnStatus, charge: billing.TurnCharge | None = None) -> None:
        super().__init__(status.value)
        self.status = status
        self.charge = charge


class ConversationTurn:
    """一轮对话的执行器。每个阶段是一个方法，阶段之间的数据显式传递，状态只有本轮累积的几项。"""

    def __init__(
        self,
        request: TurnRequest,
        services: TurnServices,
        settings: ConversationSettings,
        *,
        timer: StageTimer | None = None,
    ) -> None:
        self.request = request
        self.services = services
        self.settings = settings
        self.timer = timer or StageTimer(queue_seconds=request.queue_seconds)
        self._pending_warning: str | None = None
        self._sent_messages: list[Any] = []
        self._bot_event_message_ids: set[int] = set()

    # -- 总流程 -------------------------------------------------------------

    async def run(self) -> TurnResult:
        charge: billing.TurnCharge | None = None
        try:
            with self.timer.stage(Stage.PLAN):
                planned = await self._plan()
            with self.timer.stage(Stage.CHARGE):
                charge = await self._before_model(Stage.CHARGE, lambda: self._charge(planned))
            with self.timer.stage(Stage.CONTEXT):
                user_state_prompt = await self._before_model(
                    Stage.CONTEXT, lambda: self._load_context(charge)
                )
            with self.timer.stage(Stage.PREPARE):
                prepared = await self._before_model(
                    Stage.PREPARE, lambda: self._prepare_inputs(planned)
                )
            with self.timer.stage(Stage.HISTORY_IN):
                chat_history = await self._before_model(
                    Stage.HISTORY_IN, lambda: self._record_input(prepared, user_state_prompt)
                )
            with self.timer.stage(Stage.MODEL):
                reply = await self._run_model(prepared, chat_history, user_state_prompt)
            with self.timer.stage(Stage.HISTORY_OUT):
                await self._after_model(Stage.HISTORY_OUT, lambda: self._record_output(reply))
            with self.timer.stage(Stage.DELIVERY):
                await self._deliver(reply)
            with self.timer.stage(Stage.FINALIZE):
                await self._finalize(reply)
        except _Stop as stop:
            return TurnResult(
                status=stop.status,
                timings=self.timer.snapshot(),
                charge=stop.charge or charge,
            )
        return TurnResult(
            status=TurnStatus.COMPLETED,
            timings=self.timer.snapshot(),
            charge=charge,
            runtime_error=reply.runtime_error,
            sent_message_count=len(self._sent_messages),
        )

    # -- 整轮截止时间 ---------------------------------------------------------

    async def _before_model(self, stage: Stage, step: Callable[[], Awaitable[_T]]) -> _T:
        """模型之前的阶段（扣费、上下文、媒体、写入输入）受整轮截止时间约束。

        到期时取消正在等待的那一步（数据库、Telegram 下载都会被取消），回复超时提示并结束这一轮；
        扣费已经提交时不退（与模型阶段超时一致）。
        """
        deadline = self.request.deadline
        if deadline is None:
            return await step()
        try:
            async with deadline.guard():
                return await step()
        except DeadlineExceeded as exc:
            metrics.counter("turn.deadline_hits", reason=exc.reason, phase=stage.value).inc()
            await self._reply_deadline_notice(deadline, exc.reason)
            raise _Stop(TurnStatus.DEADLINE_EXCEEDED) from exc

    async def _after_model(self, stage: Stage, step: Callable[[], Awaitable[None]]) -> None:
        """模型之后写历史：与投递一样受截止时间加宽限约束，到期时放弃这一步，后面的阶段照常进行。"""
        deadline = self.request.deadline
        if deadline is None:
            await step()
            return
        try:
            async with deadline.guard(extra=DELIVERY_GRACE_SECONDS):
                await step()
        except DeadlineExceeded as exc:
            metrics.counter("turn.deadline_hits", reason=exc.reason, phase=stage.value).inc()
            logger.warning(
                "%s was cut short by the turn deadline (%s): user_id=%s chat_id=%s",
                stage.value,
                exc.reason,
                self.request.sender.user_id,
                self.request.chat.chat_id,
            )

    async def _reply_deadline_notice(self, deadline: Deadline, reason: str) -> None:
        """超时提示本身也只等宽限那么久，Telegram 卡住时不会让这一轮一直占着槽位。"""
        text = (
            TURN_SHUTDOWN_ERROR_MESSAGE if reason == REASON_SHUTDOWN else TURN_DEADLINE_ERROR_MESSAGE
        )
        try:
            async with deadline.guard(extra=DELIVERY_GRACE_SECONDS):
                await self.services.reply_text(self.request.reply_target, text)
        except DeadlineExceeded:
            logger.warning(
                "deadline notice could not be delivered in time: user_id=%s chat_id=%s",
                self.request.sender.user_id,
                self.request.chat.chat_id,
            )

    # -- plan ---------------------------------------------------------------

    async def _plan(self) -> list[PlannedMessage]:
        planned: list[PlannedMessage] = []
        for incoming in self.request.messages:
            message: Any = incoming.message
            if message.photo or message.sticker:
                # 媒体消息（图片或贴纸）固定价格
                cost = billing.MEDIA_COST
                is_media = True
            else:
                text = message.text
                if not text:
                    logging.warning("收到没有文本内容的消息，忽略处理")
                    continue
                if len(text) > billing.MAX_TEXT_LENGTH:
                    await self.services.reply_text(message, MESSAGE_TOO_LONG_TEXT)
                    raise _Stop(TurnStatus.MESSAGE_TOO_LONG)
                cost = billing.text_message_cost(len(text))
                is_media = False
            planned.append(
                PlannedMessage(
                    message=message,
                    cost=cost,
                    is_media=is_media,
                    edited=incoming.edited,
                    update_id=incoming.update_id,
                )
            )
        if not planned:
            raise _Stop(TurnStatus.NOTHING_TO_PROCESS)
        return planned

    # -- charge -------------------------------------------------------------

    async def _charge(self, planned: list[PlannedMessage]) -> billing.TurnCharge:
        request = self.request
        # 在扣费前写完上一项操作，避免余额恰好归零时把旧事件误判为收尾记录。
        await self.services.flush_events(request.conversation_id)
        # 上面的等待可能刚好用完了时间：到期就不再扣费。
        if request.deadline is not None:
            request.deadline.raise_if_expired()

        # 每条消息按持久身份各记一笔账，整轮同一个事务；余额不足整轮不扣、不进入本轮、不贡献奖池。
        charge = await self.services.charge(
            request.sender.user_id,
            [
                billing.TurnMessage.from_message(
                    item.message,
                    chat_id=request.chat.chat_id,
                    cost=item.cost,
                    edited=item.edited,
                    update_id=item.update_id,
                )
                for item in planned
            ],
        )
        if charge.status is billing.TurnChargeStatus.UNREGISTERED:
            await self.services.reply_text(request.reply_target, UNREGISTERED_TEXT)
            raise _Stop(TurnStatus.UNREGISTERED, charge)
        if charge.status is billing.TurnChargeStatus.INSUFFICIENT:
            await self.services.reply_text(
                request.reply_target,
                insufficient_balance_text(sum(item.cost for item in planned)),
            )
            raise _Stop(TurnStatus.INSUFFICIENT_BALANCE, charge)
        return charge

    # -- context ------------------------------------------------------------

    async def _load_context(self, charge: billing.TurnCharge) -> str:
        user_id = self.request.sender.user_id
        state = await self.services.load_user_state(user_id)
        return format_user_state_prompt(
            user_coins=charge.balance_total,
            user_plan=balance.resolve_user_plan(user_id, charge.balance_paid),
            user_permission=charge.permission,
            impression=format_impression(state.impression),
            personal_info=format_personal_info(charge.info),
            diary_exists=state.diary_exists,
        )

    # -- prepare ------------------------------------------------------------

    async def _prepare_inputs(self, planned: list[PlannedMessage]) -> _PreparedInput:
        """下载并识别媒体，整理写入历史的用户消息与只给模型看的多模态替换。"""
        prepared = _PreparedInput()
        for item in planned:
            message: Any = item.message
            format_kwargs = self._format_kwargs(message, edited=item.edited)
            command = normalize_command_name(getattr(message, "text", None))
            if command:
                format_kwargs.update({"event": "command", "command": command})

            base_kwargs = self._base_format_kwargs(message)
            if item.is_media:
                formatted_message = await self._prepare_media(
                    message,
                    base_kwargs,
                    format_kwargs,
                    prepared,
                )
            else:
                formatted_message = format_xml_message(
                    **base_kwargs,
                    message_text=message.text or "",
                    **format_kwargs,
                )

            if command != "fogmoebot":
                prepared.user_record_entries.append(("user", formatted_message))
        return prepared

    def _base_format_kwargs(self, message: Any) -> dict[str, Any]:
        chat = self.request.chat
        group_title = (chat.title or "").strip()
        return {
            "chat_type": chat.chat_type or "private",
            "chat_title": group_title or None,
            "timestamp": message_utils._format_message_timestamp(message.date)
            or time.strftime("%Y-%m-%d %H:%M:%S"),
            "user_name": self.request.sender.display_name,
        }

    @staticmethod
    def _format_kwargs(message: Any, *, edited: bool) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "message_id": getattr(message, "message_id", None),
            "edited": edited,
            "edited_at": (
                message_utils._format_message_timestamp(getattr(message, "edit_date", None))
                if edited
                else None
            ),
        }
        kwargs.update(message_utils._build_forward_format_kwargs(message))
        if message.reply_to_message:
            kwargs.update(message_utils._build_reply_format_kwargs(message.reply_to_message))
        return kwargs

    async def _prepare_media(
        self,
        message: Any,
        base_kwargs: dict[str, Any],
        format_kwargs: dict[str, Any],
        prepared: _PreparedInput,
    ) -> str:
        """下载并识别图片或贴纸，返回写入历史的描述文本；识别用的原图只进运行时消息。"""
        limit = self.settings.max_media_download_bytes
        try:
            if message.photo:
                media_type = "photo"
                file = await message.photo[-1].get_file()
                media_emoji = None
            else:
                media_type = "sticker"
                file = await message.sticker.get_file()
                media_emoji = getattr(message.sticker, "emoji", None)

            caption = message.caption if message.caption else ""

            file_size = getattr(file, "file_size", None)
            if file_size and file_size > limit:
                await self.services.reply_text(message, MEDIA_TOO_LARGE_TEXT)
                raise _Stop(TurnStatus.MEDIA_TOO_LARGE)

            # 直接下载到内存，避免把用户图片落盘。
            file_bytes = await file.download_as_bytearray()
            if len(file_bytes) > limit:
                await self.services.reply_text(message, MEDIA_TOO_LARGE_TEXT)
                raise _Stop(TurnStatus.MEDIA_TOO_LARGE)

            base64_str = base64.b64encode(file_bytes).decode("utf-8")

            image_description = await self.services.analyze_image(base64_str)

            # 组合图片描述和用户文本说明
            message_text = caption if caption else f"[{media_type}]"
            formatted_message = format_xml_message(
                **base_kwargs,
                message_text=message_text,
                **format_kwargs,
                media_type=media_type,
                media_description=image_description,
                media_emoji=media_emoji,
            )
            runtime_formatted_message = format_xml_message(
                **base_kwargs,
                message_text=message_text,
                **format_kwargs,
                media_type=media_type,
                media_emoji=media_emoji,
            )
            runtime_user_message = message_utils._build_multimodal_user_message(
                runtime_formatted_message,
                base64_str=base64_str,
                mime_type=message_utils._media_mime_type(media_type, message),
            )
            if runtime_user_message:
                prepared.runtime_replacements.append((formatted_message, runtime_user_message))
            return formatted_message
        except _Stop:
            raise
        except Exception as exc:
            logging.error("处理媒体消息时出错: %s", exc)
            await self.services.reply_text(message, MEDIA_FAILED_TEXT)
            raise _Stop(TurnStatus.MEDIA_FAILED) from exc

    # -- history_in ---------------------------------------------------------

    async def _record_input(
        self,
        prepared: _PreparedInput,
        user_state_prompt: str,
    ) -> list[dict[str, Any]]:
        request = self.request
        conversation_id = request.conversation_id
        if prepared.user_record_entries:
            # /fogmoebot 已由统一命令观察器写入；其他消息在这里批量写入。
            await self._persist_records(
                await self.services.insert_records(
                    conversation_id,
                    prepared.user_record_entries,
                    system_prompt_extra=user_state_prompt,
                    allow_zero_balance=True,
                )
            )
        if request.chat.is_private:
            await self.services.arm_idle_followup(request.sender.user_id)

        # 立即获取最新历史记录，以便 AI 能看到刚刚插入的消息
        return await self.services.get_history(conversation_id)

    # -- model --------------------------------------------------------------

    async def _run_model(
        self,
        prepared: _PreparedInput,
        chat_history: list[dict[str, Any]],
        user_state_prompt: str,
    ) -> _Reply:
        request = self.request
        services = self.services
        chat_id = request.chat.chat_id
        reply_target: Any = request.reply_target

        try:
            await services.send_typing(request.bot, chat_id)
        except Exception:
            logger.debug("Failed to send typing action before AI request")

        visible_content_handler = services.make_visible_handler(
            bot=request.bot,
            chat_id=chat_id,
            first_text_send=reply_target.reply_text,
            fallback_send=self._fallback_send(),
            reply_to_message_id=getattr(reply_target, "message_id", None),
        )

        # 单一的模型执行调用点：路由、provider 回退、工具循环都在它后面。
        with suppress_telegram_history():
            response = await services.run_model(
                ModelRequest(
                    messages=message_utils._replace_user_messages_for_ai(
                        chat_history,
                        prepared.runtime_replacements,
                    ),
                    text_fallback_messages=chat_history,
                    user_id=request.sender.user_id,
                    tool_context=build_tool_context(request, user_state_prompt),
                    visible_content_handler=visible_content_handler,
                    deadline=request.deadline,
                )
            )
        self._sent_messages.extend(getattr(visible_content_handler, "sent_messages", []))
        assistant_message = normalize_ai_reply_text(response.text)
        runtime_error = ai_chat.runtime_error_cause(assistant_message)
        if assistant_message.strip():
            assistant_message = await services.normalize_stickers(assistant_message)
        return _Reply(
            text=assistant_message,
            tool_logs=response.tool_logs,
            runtime_error=runtime_error,
            tool_record_entries=tool_logs_to_record_entries(response.tool_logs),
            completed_clear=tool_logs_completed_clear(response.tool_logs),
        )

    def _fallback_send(self) -> Any:
        return partial_send(self.request.bot.send_message, self.request.chat.chat_id)

    # -- history_out --------------------------------------------------------

    async def _record_output(self, reply: _Reply) -> None:
        conversation_id = self.request.conversation_id
        if reply.tool_record_entries and not reply.completed_clear:
            await self._persist_records(
                await self.services.insert_records(
                    conversation_id,
                    reply.tool_record_entries,
                    allow_zero_balance=True,
                )
            )

        if reply.text.strip() and not reply.runtime_error and not reply.completed_clear:
            await self._persist_records(
                await self.services.insert_record(
                    conversation_id,
                    "assistant",
                    redact_text(reply.text),
                    allow_zero_balance=True,
                )
            )

    # -- delivery -----------------------------------------------------------

    async def _deliver(self, reply: _Reply) -> None:
        """投递：受整轮截止时间加上固定宽限约束（宽限让「超时提示」本身还能发出去）。"""
        deadline = self.request.deadline
        if deadline is None:
            await self._deliver_messages(reply)
            return
        try:
            async with deadline.guard(extra=DELIVERY_GRACE_SECONDS):
                await self._deliver_messages(reply)
        except DeadlineExceeded as exc:
            metrics.counter("turn.deadline_hits", reason=exc.reason, phase="delivery").inc()
            logger.warning(
                "delivery was cut short by the turn deadline (%s): user_id=%s chat_id=%s",
                exc.reason,
                self.request.sender.user_id,
                self.request.chat.chat_id,
            )

    async def _deliver_messages(self, reply: _Reply) -> None:
        request = self.request
        services = self.services
        chat_id = request.chat.chat_id
        reply_target: Any = request.reply_target
        sent_messages = self._sent_messages

        if self._pending_warning:
            await self._notify_history_warning(self._pending_warning)

        # 发送未通过可见循环即时发送的最终回复
        if reply.text.strip():
            has_visible_message = bool(sent_messages)
            try:
                await services.send_typing(request.bot, chat_id)
            except Exception:
                logger.debug("Failed to send typing action before final AI reply")
            send_scope = (
                telegram_history_scope(
                    origin="bot_runtime",
                    event="error_notice",
                    cause=reply.runtime_error,
                    command=normalize_command_name(getattr(reply_target, "text", None)),
                )
                if reply.runtime_error
                else suppress_telegram_history()
            )
            fallback_send = self._fallback_send()
            with send_scope:
                reply_messages = await services.send_reply(
                    bot=request.bot,
                    chat_id=chat_id,
                    text=reply.text,
                    first_text_send=fallback_send if has_visible_message else reply_target.reply_text,
                    fallback_send=fallback_send,
                    reply_to_message_id=(
                        None if has_visible_message else getattr(reply_target, "message_id", None)
                    ),
                )
            sent_messages.extend(reply_messages)
            if reply.runtime_error:
                self._bot_event_message_ids.update(
                    message_id
                    for sent_message in reply_messages
                    if (message_id := getattr(sent_message, "message_id", None)) is not None
                )
        sent_messages.extend(
            await services.send_generated_media(
                bot=request.bot,
                chat_id=chat_id,
                tool_logs=reply.tool_logs,
            )
        )
        if not sent_messages and not reply.text.strip():
            tool_log_types = [
                str(tool_log.get("type", "tool_result"))
                for tool_log in reply.tool_logs
                if isinstance(tool_log, dict)
            ]
            logger.info(
                "AI produced empty response; no Telegram message sent: user_id=%s conversation_id=%s tool_log_types=%s",
                request.sender.user_id,
                request.conversation_id,
                tool_log_types,
            )
        if request.chat.is_group:
            for sent_message in sent_messages:
                if sent_message is None:
                    continue
                if getattr(sent_message, "message_id", None) in self._bot_event_message_ids:
                    continue
                await services.log_group_message(sent_message, chat_id)

    # -- finalize -----------------------------------------------------------

    async def _finalize(self, reply: _Reply) -> None:
        request = self.request
        conversation_id = request.conversation_id
        if reply.completed_clear:
            # 本轮工具调用完成后才建立真正的新会话边界。
            await self.services.archive_completed_clear(
                bot=request.bot,
                user_id=request.sender.user_id,
                conversation_id=conversation_id,
                tool_record_entries=reply.tool_record_entries,
                assistant_message=reply.text,
                runtime_error=reply.runtime_error,
            )

        # 先保存本轮所有成功显示的结果，再把零余额状态作为严格写入边界。
        await self.services.flush_events(conversation_id)
        await self._persist_records(
            await self.services.insert_records(
                conversation_id,
                [],
                suspend_if_zero=True,
            ),
            announce=True,
        )

    # -- 历史写入的收尾 -------------------------------------------------------

    async def _persist_records(
        self,
        insert_result: HistoryInsert,
        *,
        announce: bool = False,
    ) -> None:
        """统一处理一次历史写入的收尾：归档、容量提示与摘要调度。

        announce=True 时立即把容量提示发给用户，否则先记下、由本轮的 `delivery` 阶段统一提示。
        """
        request = self.request
        snapshot_created, warning_level, archived_records = insert_result
        if archived_records:
            await self.services.send_archive(
                request.bot,
                request.sender.user_id,
                archived_records,
            )
        if announce:
            await self._notify_history_warning(warning_level)
        else:
            self._pending_warning = merge_history_warning(self._pending_warning, warning_level)
        if warning_level == OVERFLOW:
            await self.services.handle_history_overflow(request.conversation_id)
        if snapshot_created and warning_level != OVERFLOW:
            self.services.schedule_summary(request.conversation_id)

    async def _notify_history_warning(self, level: str | None) -> None:
        text = history_warning_text(level)
        if text is None:
            return
        await self.services.send_warning(self.request.bot, self.request.chat.chat_id, text)


@dataclass(slots=True)
class _PreparedInput:
    """`prepare` 阶段的产物。"""

    # 写入历史的用户消息（媒体消息写的是图片描述，不含原图）。
    user_record_entries: list[tuple[str, str]] = field(default_factory=list)
    # 运行时消息替换：(历史里的文本, 带原图的多模态消息)，只给模型看。
    runtime_replacements: list[tuple[str, dict[str, Any]]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _Reply:
    """`model` 阶段的产物：规范化后的回复与由工具日志推出的历史记录。"""

    text: str
    tool_logs: list[ToolLog]
    runtime_error: str | None
    tool_record_entries: list[tuple[str, object]]
    completed_clear: bool


async def run_turn(
    request: TurnRequest,
    services: TurnServices | None = None,
    settings: ConversationSettings | None = None,
) -> TurnResult:
    """执行一轮对话并记录各阶段耗时。`services` 与 `settings` 不传时用生产实现与当前配置。"""
    turn = ConversationTurn(
        request,
        services or default_services(),
        settings or ConversationSettings.from_config(),
    )
    status = "error"
    try:
        result = await turn.run()
        status = result.status.value
        return result
    finally:
        timings = turn.timer.snapshot()
        metrics.counter("turn.finished", status=status).inc()
        metrics.histogram("turn.queue_seconds").observe(timings.queue_seconds)
        metrics.histogram("turn.run_seconds").observe(timings.run_seconds)
        metrics.histogram("turn.total_seconds").observe(
            timings.queue_seconds + timings.run_seconds
        )
        logger.info(
            "conversation turn finished: user_id=%s chat_id=%s messages=%s status=%s %s",
            request.sender.user_id,
            request.chat.chat_id,
            len(request.messages),
            status,
            turn.timer.snapshot().summary(),
        )
