"""工具循环这一轮的状态：模型看到的消息序列、每次工具调用的记录与已经发给用户的内容。

循环、投递和截止时间收尾都改这一份状态，结束（或带着异常离开）时由 `result` 冻结成 `LoopResult`。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any

from .tool_loop_messages import tool_result_json
from .types import (
    LoopResult,
    LoopStatus,
    MediaDeliveryState,
    ToolCallRecord,
    ToolCallStatus,
)


@dataclass(slots=True)
class LoopState:
    """`messages` 是发给模型的完整序列（系统提示词 + 历史 + 本轮），本轮从 `turn_start` 开始。"""

    messages: list[dict[str, Any]]
    turn_start: int
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    delivered_contents: list[str] = field(default_factory=list)

    @property
    def has_side_effects(self) -> bool:
        """已经开始执行工具或向用户发出过内容；见 `LoopResult.has_side_effects`。"""
        return bool(self.tool_calls or self.delivered_contents)

    def turn_messages(self) -> list[dict[str, Any]]:
        return list(self.messages[self.turn_start :])

    def result(
        self,
        text: str,
        status: LoopStatus = LoopStatus.COMPLETED,
        *,
        error: BaseException | None = None,
    ) -> LoopResult:
        return LoopResult(
            text=text,
            status=status,
            tool_calls=tuple(self.tool_calls),
            delivered_contents=tuple(self.delivered_contents),
            turn_messages=self.turn_messages(),
            error=error,
        )

    def record_delivered(self, content: str, *, append_message: bool) -> None:
        """记下已经发给用户的可见文字；`append_message` 时它单独成为一条 assistant 消息。"""
        self.delivered_contents.append(content)
        if append_message:
            self.messages.append({"role": "assistant", "content": content})

    def start_tool_call(
        self,
        tool_name: str,
        tool_call_id: str,
        arguments: Any,
    ) -> int:
        """开始处理一次工具调用之前登记，返回它在 `tool_calls` 里的位置。

        先登记再执行：之后的任何异常都发生在「已有副作用」之后，调用方不能再从头重跑这一轮。
        """
        self.tool_calls.append(ToolCallRecord(tool_name, tool_call_id, arguments))
        return len(self.tool_calls) - 1

    def finish_tool_call(
        self,
        index: int,
        *,
        status: ToolCallStatus,
        public_result: Any,
        internal_result: Any = None,
        media_delivery: MediaDeliveryState = MediaDeliveryState.NOT_APPLICABLE,
    ) -> None:
        """补上 `index` 那次调用的结果，并把交回模型的结果追加到消息序列（与 assistant 的 tool_call 配对）。"""
        record = dataclasses.replace(
            self.tool_calls[index],
            status=status,
            public_result=public_result,
            internal_result=internal_result,
            media_delivery=media_delivery,
        )
        self.tool_calls[index] = record
        self.messages.append(
            {
                "role": "tool",
                "tool_call_id": record.tool_call_id,
                "name": record.tool_name,
                "content": tool_result_json(public_result),
            }
        )

    def add_context_messages(self, context_messages: list[dict[str, str]]) -> None:
        """这一轮工具带回来的 Telegram 事件（例如代执行命令的回复），在本轮所有 tool 结果之后进入序列。"""
        self.messages.extend(context_messages)
