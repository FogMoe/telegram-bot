"""工具循环：问模型 → 解析 tool_calls → 执行工具 → 回填结果 → 下一轮，直到模型给出不带工具调用的回复。

这里放循环骨架与模型调用（含工具执行后的重试），以及按 agent 回退运行一次循环的 `run_agent`。
agent 的声明（工具、系统提示词、循环参数）在 `agents.py`。细节在同目录的模块里：

- `tool_loop_state.py`：这一轮的消息序列、每次工具调用的记录与已发出的内容，结束时冻结成 `LoopResult`；
- `tool_loop_messages.py`：模型返回的 assistant 消息与 tool_calls 转成普通 dict，以及 provider 的协议兼容；
- `tool_loop_execution.py`：一次工具调用的参数校验、执行、日志与指标；
- `tool_loop_delivery.py`：可见内容与生成媒体立刻发到 Telegram，交回模型的公开工具结果；
- `tool_loop_deadline.py`：整轮截止时间在工具中途到期时，给这一轮补齐结果，保持历史配对。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Any

from fogmoe_telegram_bot.core import ai_providers, config
from fogmoe_telegram_bot.core.deadline import REASON_SHUTDOWN, Deadline, DeadlineExceeded

from .agents import MAIN_AGENT, AgentSpec
from .errors import is_retryable_completion_error
from .litellm_client import create_chat_completion, response_token_counts
from .task_runner import Candidate, run_with_fallback
from .tool_loop_deadline import close_interrupted_round, deadline_guard
from .tool_loop_delivery import (
    close_round_after_media_deadline,
    context_messages_from_tool_result,
    emit_visible_content,
    media_delivery_after_execution,
    public_tool_result,
    return_final_text_response,
    send_media_result_immediately,
)
from .tool_loop_execution import execute_tool_call, parse_tool_arguments
from .tool_loop_messages import (
    assistant_message_to_plain,
    normalise_tool_calls,
    resolve_assistant_message,
)
from .tool_loop_state import LoopState
from .tools import ToolSpec
from .tools.context import tool_request_scope
from .types import (
    LoopResult,
    LoopStatus,
    PartialAIResponseError,
    ToolCallStatus,
    TurnDeadlineError,
    VisibleContentHandler,
    raise_if_aborted,
)

POST_TOOL_COMPLETION_RETRY_DELAYS_SECONDS = (1.0, 3.0)


async def _create_chat_completion_with_post_tool_retries(
    provider: str,
    model: str,
    *,
    messages: list[dict[str, Any]],
    request_kwargs: dict[str, Any],
    provider_name: str,
    has_side_effects: bool,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, object] | None = None,
) -> Any:
    call_kwargs = dict(request_kwargs)
    if tools is not None:
        call_kwargs["tools"] = tools
    if tool_choice is not None:
        call_kwargs["tool_choice"] = tool_choice

    retry_delays = POST_TOOL_COMPLETION_RETRY_DELAYS_SECONDS if has_side_effects else ()
    retry_index = 0

    while True:
        try:
            return await create_chat_completion(
                provider,
                model,
                messages=messages,
                **call_kwargs,
            )
        except Exception as exc:
            if (
                retry_index >= len(retry_delays)
                or not is_retryable_completion_error(exc)
            ):
                raise

            delay = retry_delays[retry_index]
            retry_index += 1
            logging.warning(
                "%s 工具执行后的回复生成遇到临时错误，%.1f 秒后进行第 %s/%s 次重试: %s",
                provider_name,
                delay,
                retry_index,
                len(retry_delays),
                exc,
            )
            await asyncio.sleep(delay)


async def run_tool_loop(
    provider: str,
    model: str,
    messages: list[dict[str, Any]],
    tool_context: dict[str, object] | None = None,
    *,
    agent: AgentSpec = MAIN_AGENT,
    provider_name: str | None = None,
    visible_content_handler: VisibleContentHandler | None = None,
    deadline: Deadline | None = None,
    tools: Sequence[ToolSpec] | None = None,
) -> LoopResult:
    """用 `provider` 的 `model` 为 `agent` 跑一次工具循环，返回类型化的 `LoopResult`。

    工具默认是 `agent.tools(provider 的声明)`；`provider_name` 是日志前缀，默认用 `agent.display_name`。
    原生 async：模型调用 `await`，async 工具直接 `await`，同步工具走有界线程适配器。
    `deadline` 到期时取消正在等待的步骤，抛 `TurnDeadlineError`（带着到期为止的结果）。
    开始执行工具或发出过可见内容之后（`LoopState.has_side_effects`），其他异常一律包成
    `PartialAIResponseError`（带着到失败为止的结果）离开循环：调用方不能再换模型或 provider 从头重跑。
    """
    provider_name = provider_name or agent.display_name
    tool_specs = agent.tools(ai_providers.lookup(provider)) if tools is None else tuple(tools)
    tools_by_name = {spec.name: spec for spec in tool_specs}
    tool_schemas = [spec.schema() for spec in tool_specs]
    system_message = {"role": "system", "content": agent.system_prompt(tool_context)}

    filtered_messages = [
        msg for msg in messages if msg.get("content") is not None or msg.get("tool_calls")
    ]
    filtered_messages.insert(0, system_message)
    state = LoopState(messages=filtered_messages, turn_start=len(filtered_messages))

    # 本次循环的模型调用次数与 token 合计（输入、其中命中缓存的、输出），结束时写一行日志。
    ai_calls = 0
    token_totals = [0, 0, 0]
    request_timeout: float | None = (
        config.AI_CHAT_COMPLETION_TIMEOUT_SECONDS
        if agent.completion_timeout is None
        else agent.completion_timeout
    )

    async def complete(
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, object] | None = None,
    ) -> Any:
        """一次模型调用：整轮截止时间到期就取消，单次超时收紧到剩余时间之内。"""
        call_timeout = request_timeout if deadline is None else deadline.clip(request_timeout)
        request_kwargs = {
            **agent.completion_kwargs,
            "context_hard_limit_ratio": agent.context_hard_limit_ratio,
            "timeout": call_timeout,
        }
        nonlocal ai_calls
        async with deadline_guard(deadline):
            response = await _create_chat_completion_with_post_tool_retries(
                provider,
                model,
                messages=state.messages,
                request_kwargs=request_kwargs,
                provider_name=provider_name,
                has_side_effects=state.has_side_effects,
                tools=tools,
                tool_choice=tool_choice,
            )
        ai_calls += 1
        for index, count in enumerate(response_token_counts(response)):
            token_totals[index] += count
        return response

    # 到期时正在等待的步骤，决定提示和是否把这次计为 provider 失败。
    phase = "model"
    try:
        for iteration in range(agent.max_iterations):
            raise_if_aborted(tool_context)
            phase = "model"
            if deadline is not None:
                deadline.raise_if_expired()
            response = await complete(tools=tool_schemas, tool_choice="auto")

            assistant_message, raw_tool_calls = resolve_assistant_message(
                response,
                provider=provider,
                provider_name=provider_name,
            )
            assistant_content = assistant_message.content or ""

            if not raw_tool_calls:
                logging.info("%s 第 %s 轮：无工具调用，直接返回答案", provider_name, iteration + 1)
                phase = "delivery"
                return await return_final_text_response(
                    state,
                    content_text=assistant_content,
                    visible_content_handler=visible_content_handler,
                    provider_name=provider_name,
                    deadline=deadline,
                    status=LoopStatus.COMPLETED,
                )

            tool_calls = normalise_tool_calls(raw_tool_calls)
            logging.info("%s 第 %s 轮：检测到 %s 个工具调用", provider_name, iteration + 1, len(tool_calls))

            assistant_content_for_model = assistant_content
            if visible_content_handler is not None and assistant_content.strip():
                phase = "delivery"
                visible_result = await emit_visible_content(
                    visible_content_handler,
                    assistant_content,
                    provider_name=provider_name,
                    deadline=deadline,
                )
                if visible_result.content:
                    assistant_content_for_model = visible_result.content
                    # 只发出了一部分：这一轮到此为止，发出的部分单独成为一条 assistant 消息，工具不执行。
                    state.record_delivered(
                        visible_result.content,
                        append_message=not visible_result.completed,
                    )
                    if not visible_result.completed:
                        return state.result("")

            raise_if_aborted(tool_context)
            state.messages.append(
                assistant_message_to_plain(
                    assistant_message,
                    content=assistant_content_for_model,
                    tool_calls=tool_calls,
                )
            )

            round_context_messages: list[dict[str, str]] = []
            for call_index, tool_call in enumerate(tool_calls):
                function_payload = tool_call.get("function") or {}
                function_name = str(function_payload.get("name") or "").strip()

                # 后台任务被撤销后不再执行剩余的工具。
                raise_if_aborted(tool_context)

                arguments = parse_tool_arguments(
                    function_name,
                    function_payload,
                    provider_name=provider_name,
                )
                record_index = state.start_tool_call(
                    function_name,
                    tool_call["id"],
                    arguments.logged,
                )

                # 只有真正运行处理函数时才会在这里到期；校验失败、未开放、未知工具都直接返回错误结果。
                phase = "tool"
                try:
                    outcome = await execute_tool_call(
                        function_name,
                        arguments,
                        tools=tools_by_name,
                        provider_name=provider_name,
                        deadline=deadline,
                    )
                except DeadlineExceeded as exc:
                    close_interrupted_round(
                        state,
                        tool_calls,
                        call_index + 1,
                        exc.reason,
                        round_context_messages,
                        running_record=record_index,
                    )
                    raise

                phase = "delivery"
                try:
                    sent_media_messages = await send_media_result_immediately(
                        visible_content_handler=visible_content_handler,
                        tool_name=function_name,
                        tool_result=outcome.result,
                        provider_name=provider_name,
                        deadline=deadline,
                    )
                except DeadlineExceeded as exc:
                    close_round_after_media_deadline(
                        state,
                        record_index,
                        tool_calls,
                        call_index,
                        exc.reason,
                        round_context_messages,
                        internal_tool_result=outcome.result,
                    )
                    raise

                state.finish_tool_call(
                    record_index,
                    status=outcome.status,
                    public_result=public_tool_result(
                        function_name,
                        outcome.result,
                        media_sent=bool(sent_media_messages),
                    ),
                    internal_result=outcome.result,
                    media_delivery=media_delivery_after_execution(
                        function_name,
                        outcome.result,
                        sent_media_messages,
                    ),
                )
                round_context_messages.extend(
                    context_messages_from_tool_result(outcome.result)
                )

            state.add_context_messages(round_context_messages)

        logging.warning("%s 工具调用次数超限（%s轮）", provider_name, agent.max_iterations)
        phase = "model"
        if deadline is not None:
            deadline.raise_if_expired()
        response = await complete()

        assistant_message, raw_tool_calls = resolve_assistant_message(
            response,
            provider=provider,
            provider_name=provider_name,
        )
        if raw_tool_calls:
            logging.warning(
                "%s 工具调用超限后的最终回复仍包含工具调用，忽略工具调用并使用文本内容。",
                provider_name,
            )
        phase = "delivery"
        return await return_final_text_response(
            state,
            content_text=assistant_message.content or "",
            visible_content_handler=visible_content_handler,
            provider_name=provider_name,
            deadline=deadline,
            status=LoopStatus.TOOL_LIMIT_REACHED,
        )
    except DeadlineExceeded as exc:
        status = (
            LoopStatus.SHUTDOWN if exc.reason == REASON_SHUTDOWN else LoopStatus.DEADLINE_EXCEEDED
        )
        raise TurnDeadlineError(exc.reason, phase, state.result("", status)) from exc
    except Exception as exc:
        if state.has_side_effects:
            raise PartialAIResponseError(
                str(exc),
                state.result("", LoopStatus.PARTIAL_FAILURE),
            ) from exc
        raise
    finally:
        logging.info(
            "%s tool loop finished: model=%s user_id=%s ai_calls=%s in_tokens=%s "
            "cached_tokens=%s out_tokens=%s tool_calls=%s",
            provider_name,
            model,
            (tool_context or {}).get("user_id"),
            ai_calls,
            *token_totals,
            sum(1 for record in state.tool_calls if record.status is not ToolCallStatus.NOT_EXECUTED),
        )


async def run_agent(
    agent: AgentSpec,
    messages: list[dict[str, Any]],
    tool_context: dict[str, object],
) -> tuple[LoopResult, Candidate]:
    """按 `agent.task` 的 provider 与模型顺序跑一次工具循环，返回第一次成功的结果与它用的模型。

    每次尝试在一份新的工具请求上下文里运行（`tool_request_scope`）。工具都是只读的 agent，执行过工具之后的
    失败也换下一个模型重跑（`AgentSpec.tools_are_read_only`）；声明了 `empty_reply_is_failure` 的 agent
    空回复也算这次尝试失败。全部失败时抛 `AllProvidersFailedError`。
    后台 agent 用它；主聊天的回退另有熔断与看图降级，在 router 里。
    """

    async def attempt(candidate: Candidate) -> tuple[LoopResult, Candidate]:
        with tool_request_scope(tool_context):
            result = await run_tool_loop(
                candidate.provider,
                candidate.model,
                messages,
                tool_context,
                agent=agent,
            )
        if agent.empty_reply_is_failure and not result.text.strip():
            raise ValueError(f"{agent.display_name} model returned empty content")
        return result, candidate

    return await run_with_fallback(
        agent.task,
        attempt,
        tools_are_read_only=agent.tools_are_read_only,
    )
