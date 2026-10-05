"""一轮对话的类型化边界：输入、结果、阶段计时，以及模型执行的请求与响应。

`turn.py` 的业务操作只通过这里的类型与外界交换数据；Telegram handler 负责把 `Update`
映射成 `TurnRequest`，并把 `TurnResult` 当作这一轮的结论使用。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from telegram import Bot, Message

from fogmoe_telegram_bot.core import config
from fogmoe_telegram_bot.core.deadline import Deadline
from fogmoe_telegram_bot.features.ai.types import ToolLog, VisibleContentHandler

from . import billing

# 一次历史写入的结果：(是否生成了快照, 容量提示级别, 因溢出而归档的记录)。
HistoryInsert = tuple[bool, str | None, list[Any]]


@dataclass(frozen=True, slots=True)
class ConversationSettings:
    """对话入口读取的配置。由 `from_config` 在调用时取值，能看到 `config.install_settings`。"""

    batch_window_seconds: float = 1.0
    max_media_download_bytes: int = 8 * 1024 * 1024

    @classmethod
    def from_config(cls, source: Any = None) -> ConversationSettings:
        settings = config if source is None else source
        return cls(batch_window_seconds=float(settings.CHAT_BATCH_WINDOW_SECONDS))


@dataclass(frozen=True, slots=True)
class ChatRef:
    chat_id: int
    chat_type: str | None
    title: str | None = None

    @property
    def is_group(self) -> bool:
        return self.chat_type in ("group", "supergroup")

    @property
    def is_private(self) -> bool:
        return self.chat_type == "private"


@dataclass(frozen=True, slots=True)
class SenderRef:
    user_id: int
    username: str | None = None
    first_name: str | None = None
    language_code: str | None = None

    @property
    def display_name(self) -> str:
        return self.username or "EmptyUsername"


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    message: Message
    edited: bool = False
    update_id: int | None = None


@dataclass(frozen=True, slots=True)
class TurnRequest:
    """一轮对话的输入：已经通过群聊触发判断与冷却的一批消息（按时间排序，至少一条）。"""

    chat: ChatRef
    sender: SenderRef
    messages: tuple[IncomingMessage, ...]
    bot: Bot
    # 从进入对话入口到拿到会话锁、全局槽位的等待时间，只用于计时。
    queue_seconds: float = 0.0
    # 整轮截止时间：从进入队列开始计时，覆盖排队、provider 回退、工具与投递。None 表示不设截止时间。
    deadline: Deadline | None = None

    @property
    def conversation_id(self) -> int:
        return self.sender.user_id

    @property
    def reply_target(self) -> Message:
        """回复挂在这一批里最后一条消息上。"""
        return self.messages[-1].message


@dataclass(frozen=True, slots=True)
class PlannedMessage:
    """通过长度检查、已定价的一条消息。"""

    message: Message
    cost: int
    is_media: bool
    edited: bool
    update_id: int | None


@dataclass(frozen=True, slots=True)
class UserStateRecord:
    """拼用户状态提示词需要、但不在扣费结果里的两项读取。"""

    impression: str | None
    diary_exists: bool


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """交给模型执行阶段的全部输入。"""

    messages: list[dict[str, Any]]  # 运行时历史：媒体消息带图片
    text_fallback_messages: list[dict[str, Any]]  # 纯文本历史：图片已换成文字描述
    user_id: int
    tool_context: dict[str, object]
    visible_content_handler: VisibleContentHandler | None
    deadline: Deadline | None = None


@dataclass(frozen=True, slots=True)
class ModelResponse:
    text: str
    tool_logs: list[ToolLog]


class TurnStatus(StrEnum):
    COMPLETED = "completed"
    NOTHING_TO_PROCESS = "nothing_to_process"  # 没有可处理的消息：不扣费
    MESSAGE_TOO_LONG = "message_too_long"  # 已回复提示：不扣费
    UNREGISTERED = "unregistered"  # 已回复提示：不扣费
    INSUFFICIENT_BALANCE = "insufficient_balance"  # 已回复提示：整轮回滚，不扣费
    MEDIA_TOO_LARGE = "media_too_large"  # 已回复提示：这一轮已扣费，不退
    MEDIA_FAILED = "media_failed"  # 已回复提示：这一轮已扣费，不退
    # 模型之前的阶段截止时间到期：已回复提示；到期发生在扣费之后则这一轮已扣费，不退
    DEADLINE_EXCEEDED = "deadline_exceeded"


class Stage(StrEnum):
    """阶段名，也是 `TurnTimings` 的键。"""

    PLAN = "plan"
    CHARGE = "charge"
    CONTEXT = "context"
    PREPARE = "prepare"
    HISTORY_IN = "history_in"
    MODEL = "model"
    HISTORY_OUT = "history_out"
    DELIVERY = "delivery"
    FINALIZE = "finalize"


@dataclass(frozen=True, slots=True)
class TurnTimings:
    queue_seconds: float
    stages: Mapping[Stage, float]
    run_seconds: float

    def seconds(self, stage: Stage) -> float:
        return self.stages.get(stage, 0.0)

    def summary(self) -> str:
        parts = [f"queue={self.queue_seconds:.3f}s"]
        parts.extend(f"{stage.value}={self.seconds(stage):.3f}s" for stage in Stage)
        parts.append(f"total={self.run_seconds:.3f}s")
        return " ".join(parts)


@dataclass(frozen=True, slots=True)
class TurnResult:
    status: TurnStatus
    timings: TurnTimings
    charge: billing.TurnCharge | None = None
    runtime_error: str | None = None  # `ai_chat.runtime_error_cause` 的结论
    sent_message_count: int = 0


@dataclass(slots=True)
class StageTimer:
    """记录每个阶段的耗时。时钟可注入，测试里用确定的时间。"""

    queue_seconds: float = 0.0
    clock: Callable[[], float] = time.perf_counter
    _stages: dict[Stage, float] = field(default_factory=dict, init=False)
    _started: float | None = field(default=None, init=False)

    @contextmanager
    def stage(self, name: Stage) -> Iterator[None]:
        if self._started is None:
            self._started = self.clock()
        start = self.clock()
        try:
            yield
        finally:
            self._stages[name] = self._stages.get(name, 0.0) + (self.clock() - start)

    def snapshot(self) -> TurnTimings:
        run_seconds = 0.0 if self._started is None else self.clock() - self._started
        return TurnTimings(
            queue_seconds=self.queue_seconds,
            stages=dict(self._stages),
            run_seconds=run_seconds,
        )
