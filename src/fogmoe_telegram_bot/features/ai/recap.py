"""空闲跟进之前的短期回顾：回顾 agent 的声明与一次回顾的生成。

回顾只用只读工具（查摘要、搜归档、读日记），所以执行过工具之后的失败也可以换下一个模型重跑，
崩溃后也可以整段重跑。什么时候生成回顾、生成的回顾怎样进入主模型的这一轮，见空闲跟进入口。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from fogmoe_telegram_bot.core import config

from .agents import AgentSpec
from .tool_runner import run_agent

logger = logging.getLogger(__name__)

IDLE_RECAP_RETRY_LIMIT = 2
IDLE_RECAP_TIMEOUT_SECONDS = 120


class IdleRecapMemorySuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    impression: str = Field(max_length=2000)
    diary: str = Field(max_length=2000)


class IdleRecapOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recap: str = Field(max_length=2000)
    open_loops: str = Field(max_length=2000)
    suggested_follow_up: str = Field(max_length=2000)
    memory_suggestion: IdleRecapMemorySuggestion


RECAP_AGENT = AgentSpec(
    name="recap",
    task="recap",
    display_name="Idle recap",
    system_prompt=lambda _tool_context: config.IDLE_RECAP_SYSTEM_PROMPT,
    completion_timeout=IDLE_RECAP_TIMEOUT_SECONDS,
    completion_kwargs={
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "idle_recap",
                "strict": True,
                "schema": IdleRecapOutput.model_json_schema(),
            },
        },
        "drop_params": False,
    },
)


def normalize_recap_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def parse_recap_response(value: object) -> dict[str, Any]:
    text = str(value or "").strip()
    if not text:
        raise ValueError("idle recap response was empty")
    try:
        parsed = IdleRecapOutput.model_validate_json(text)
    except ValidationError as exc:
        raise ValueError("idle recap response failed the required JSON schema") from exc

    result: dict[str, Any] = {
        "recap": normalize_recap_text(parsed.recap),
        "open_loops": normalize_recap_text(parsed.open_loops),
        "suggested_follow_up": normalize_recap_text(parsed.suggested_follow_up),
        "memory_suggestion": {
            "impression": normalize_recap_text(parsed.memory_suggestion.impression),
            "diary": normalize_recap_text(parsed.memory_suggestion.diary),
        },
    }

    memory_suggestion = result["memory_suggestion"]
    if not any(
        [
            result["recap"],
            result["open_loops"],
            result["suggested_follow_up"],
            memory_suggestion["impression"],
            memory_suggestion["diary"],
        ]
    ):
        raise ValueError("idle recap response contains no usable content")
    return result


async def run_recap_agent(messages: list[dict[str, Any]], user_id: int) -> str:
    """按回顾任务的 provider 与模型顺序跑一次回顾 agent，返回模型的原始回复。"""
    result, _candidate = await run_agent(RECAP_AGENT, messages, {"user_id": user_id})
    return result.text


async def generate_recap(
    user_id: int,
    dialogue: list[dict[str, str]],
    memory_context: dict[str, Any],
) -> dict[str, Any]:
    """生成一次回顾；整段（每次都把 provider 顺序走一遍）最多重试 `IDLE_RECAP_RETRY_LIMIT` 次。"""
    transcript = json.dumps(dialogue, ensure_ascii=False)
    stored_memory = json.dumps(memory_context, ensure_ascii=False)
    prompt = (
        "根据下面近期对话生成一次短期回顾。"
        "现有长期记忆只用于判断候选内容是否已经记录。\n\n"
        f"近期对话：{transcript}\n\n"
        f"现有长期记忆：{stored_memory}"
    )
    messages = [{"role": "user", "content": prompt}]

    last_error: Exception | None = None
    for attempt in range(1, IDLE_RECAP_RETRY_LIMIT + 1):
        try:
            content = await run_recap_agent(messages, user_id)
            return parse_recap_response(content)
        except Exception as exc:
            last_error = exc
            logger.warning(
                "Idle recap generation attempt %s/%s failed: %s",
                attempt,
                IDLE_RECAP_RETRY_LIMIT,
                exc,
            )
    raise RuntimeError("Idle recap generation failed after retries") from last_error


__all__ = [
    "IDLE_RECAP_RETRY_LIMIT",
    "IDLE_RECAP_TIMEOUT_SECONDS",
    "IdleRecapMemorySuggestion",
    "IdleRecapOutput",
    "RECAP_AGENT",
    "generate_recap",
    "normalize_recap_text",
    "parse_recap_response",
    "run_recap_agent",
]
