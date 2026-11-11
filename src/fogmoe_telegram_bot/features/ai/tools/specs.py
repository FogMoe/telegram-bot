"""所有 AI 工具的单点声明表（`TOOL_SPECS`）。

新增一个工具只需要在 `TOOL_SPECS` 里加一项：参数模型、schema、处理函数、所属 agent、副作用分级、
是否已配置凭据以及媒体即时投递协议都由这里派生，不再跨多文件重复登记。

暴露给模型的工具列表只由配置决定，不按入口或聊天类型裁剪：历史在私聊、群聊和后台轮之间共享，工具调用
会留在历史里，列表按轮变化会让模型看到自己调用过的工具消失。不适用的场景由 handler 返回错误。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from fogmoe_telegram_bot.core import ai_providers, config

from ..generated_audio_sender import (
    send_generated_audio_from_tool_logs,
    send_generated_audio_from_tool_result,
)
from ..generated_image_sender import (
    send_generated_images_from_tool_logs,
    send_generated_images_from_tool_result,
)
from . import advisor_tools, code_tools, http_tools
from .advisor_tools import advisor_tool
from .code_tools import execute_python_code_tool
from .doc_tools import read_doc_tool
from .http_tools import fetch_url_tool, google_search_tool
from .image_tools import generate_image_tool
from .memory_tools import (
    fetch_group_context_tool,
    fetch_permanent_summaries_tool,
    get_help_text_tool,
    read_diary_page_tool,
    search_permanent_records_tool,
    user_diary_tool,
)
from .models import (
    AI_TOOL_ARG_MODELS,
    AdvisorArgs,
    ExecutePythonCodeArgs,
    ExecuteTelegramCommandArgs,
    FetchGroupContextArgs,
    FetchPermanentSummariesArgs,
    FetchUrlArgs,
    GenerateImageArgs,
    GenerateVoiceArgs,
    GetHelpTextArgs,
    GoogleSearchArgs,
    KindnessGiftArgs,
    LinuxSandboxArgs,
    ListAvailableStickersArgs,
    ReadDiaryPageArgs,
    ReadDocArgs,
    ScheduleAIMessageArgs,
    SearchPermanentRecordsArgs,
    SummarySearchPriorContextArgs,
    ToolArguments,
    UpdateImpressionArgs,
    UserDiaryArgs,
    parameters_schema,
)
from .sandbox_tools import linux_sandbox_tool
from .schedule_tools import schedule_ai_message_tool
from .sticker_tools import list_available_stickers_tool
from .summary_tools import search_prior_context_tool
from .telegram_command_tools import execute_telegram_command_tool
from .user_tools import kindness_gift_tool, update_impression_tool
from .voice_tools import generate_voice_tool, resolve_fish_audio_settings


class SideEffectKind(StrEnum):
    """工具副作用分级，用于区分纯只读查询与不可跨 provider 重跑的状态/投递动作。"""

    READ_ONLY = "read_only"
    EXTERNAL_COMPUTE = "external_compute"
    STATE_MUTATION = "state_mutation"
    TELEGRAM_MEDIA = "telegram_media"
    COMMAND_DELEGATION = "command_delegation"


@dataclass(frozen=True, slots=True)
class MediaToolSpec:
    """生成媒体并发往 Telegram 的工具协议（即时发送、兜底批量发送、公开结果脱敏、日志）。"""

    chat_action: str
    delivery_unknown_message: str
    public_result: Callable[[dict[str, Any], bool], dict[str, Any]]
    log_result: Callable[[str, Any], None]
    send_from_result: Callable[..., Awaitable[list[Any]]]
    send_from_logs: Callable[..., Awaitable[list[Any]]]
    batch_order: int = 100


def _media_error_public_result(tool_result: dict[str, Any]) -> dict[str, Any]:
    public_result: dict[str, Any] = {"error": tool_result.get("error")}
    for key in ("status_code", "details", "response_preview", "warnings", "retry_after_seconds"):
        if key in tool_result:
            public_result[key] = tool_result[key]
    return public_result


def _image_public_result(tool_result: dict[str, Any], media_sent: bool) -> dict[str, Any]:
    if tool_result.get("error"):
        return _media_error_public_result(tool_result)
    if tool_result.get("status") == "generated":
        return {
            "status": "generated",
            "message": (
                "Generated image has been sent to Telegram."
                if media_sent
                else (
                    "Generated image is ready and will be sent to Telegram. "
                    "If you need to inspect the image yourself later, ask the user "
                    "to forward the sent image back to you."
                )
            ),
        }
    return {"status": tool_result.get("status") or "unknown"}


def _voice_public_result(tool_result: dict[str, Any], media_sent: bool) -> dict[str, Any]:
    if tool_result.get("error"):
        return _media_error_public_result(tool_result)
    if tool_result.get("status") == "generated":
        return {
            "status": "generated",
            "message": (
                "Generated audio has been sent to Telegram."
                if media_sent
                else "Generated audio is ready and will be sent to Telegram."
            ),
        }
    return {"status": tool_result.get("status") or "unknown"}


def _log_image_tool_result(provider_name: str, tool_result: Any) -> None:
    if not isinstance(tool_result, dict):
        logging.warning(
            "%s generate_image returned non-dict result: %s",
            provider_name,
            type(tool_result).__name__,
        )
        return
    if tool_result.get("error"):
        logging.warning(
            "%s generate_image returned error: error=%s, status_code=%s, retry_after_seconds=%s, details=%s",
            provider_name,
            tool_result.get("error"),
            tool_result.get("status_code"),
            tool_result.get("retry_after_seconds"),
            str(tool_result.get("details") or tool_result.get("response_preview") or "")[:500],
        )
        return
    if tool_result.get("status") == "generated":
        images = [tool_result["image"]] if isinstance(tool_result.get("image"), dict) else []
        if not images and isinstance(tool_result.get("images"), list):
            images = tool_result["images"]
        logging.info(
            "%s generate_image generated %s image(s): count=%s, warnings=%s",
            provider_name,
            len(images),
            tool_result.get("count"),
            tool_result.get("warnings"),
        )
        return
    logging.info(
        "%s generate_image returned status=%s",
        provider_name,
        tool_result.get("status") or "unknown",
    )


def _log_voice_tool_result(provider_name: str, tool_result: Any) -> None:
    if not isinstance(tool_result, dict):
        logging.warning(
            "%s generate_voice returned non-dict result: %s",
            provider_name,
            type(tool_result).__name__,
        )
        return
    if tool_result.get("error"):
        logging.warning(
            "%s generate_voice returned error: error=%s, status_code=%s, retry_after_seconds=%s, details=%s",
            provider_name,
            tool_result.get("error"),
            tool_result.get("status_code"),
            tool_result.get("retry_after_seconds"),
            str(tool_result.get("details") or tool_result.get("response_preview") or "")[:500],
        )
        return
    if tool_result.get("status") == "generated":
        raw_audios = tool_result.get("audios")
        audios = raw_audios if isinstance(raw_audios, list) else []
        logging.info(
            "%s generate_voice generated %s audio clip(s): count=%s, warnings=%s",
            provider_name,
            len(audios),
            tool_result.get("count"),
            tool_result.get("warnings"),
        )
        return
    logging.info(
        "%s generate_voice returned status=%s",
        provider_name,
        tool_result.get("status") or "unknown",
    )


IMAGE_MEDIA_SPEC = MediaToolSpec(
    chat_action="upload_photo",
    delivery_unknown_message=(
        "The image was generated, but the turn was stopped while it was being sent to "
        "Telegram; it may or may not have arrived. Do not generate it again unless the "
        "user asks."
    ),
    public_result=_image_public_result,
    log_result=_log_image_tool_result,
    send_from_result=send_generated_images_from_tool_result,
    send_from_logs=send_generated_images_from_tool_logs,
    batch_order=20,
)

VOICE_MEDIA_SPEC = MediaToolSpec(
    chat_action="upload_voice",
    delivery_unknown_message=(
        "The audio was generated, but the turn was stopped while it was being sent to "
        "Telegram; it may or may not have arrived. Do not generate it again unless the "
        "user asks."
    ),
    public_result=_voice_public_result,
    log_result=_log_voice_tool_result,
    send_from_result=send_generated_audio_from_tool_result,
    send_from_logs=send_generated_audio_from_tool_logs,
    batch_order=10,
)


def _always_configured(_settings: Any = None) -> bool:
    return True


def _settings_source(settings: Any = None) -> Any:
    return config if settings is None else settings


def _serpapi_configured(settings: Any = None) -> bool:
    return bool(http_tools._serpapi_api_key(settings))


def _advisor_configured(settings: Any = None) -> bool:
    if settings is None:
        return advisor_tools._configured_advisor_available()
    return advisor_tools._configured_advisor_available(settings)


def _judge0_configured(settings: Any = None) -> bool:
    return bool(code_tools._judge0_api_url(settings))


def _sandbox_configured(settings: Any = None) -> bool:
    source = _settings_source(settings)
    return bool(str(getattr(source, "E2B_API_KEY", None) or "").strip())


def _image_gen_configured(settings: Any = None) -> bool:
    source = _settings_source(settings)
    url = str(getattr(source, "IMAGE_GEN_API_URL", None) or "").strip()
    token = str(getattr(source, "IMAGE_GEN_API_TOKEN", None) or "").strip()
    return bool(url and token)


def _voice_gen_configured(settings: Any = None) -> bool:
    # 与 generate_voice_tool 共用同一份解析：模型与参考音色留空时用默认值，只有 API key 是必需的。
    api_key, model, reference_id = resolve_fish_audio_settings(settings)
    return bool(api_key and model and reference_id)


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """单个 AI 工具的完整声明。"""

    name: str
    description: str
    arg_model: type[ToolArguments]
    handler: Callable[..., Any]
    agents: tuple[str, ...]
    side_effect: SideEffectKind = SideEffectKind.READ_ONLY
    is_configured: Callable[[Any], bool] = _always_configured
    media: MediaToolSpec | None = None

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": parameters_schema(self.arg_model),
            },
        }


TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="get_help_text",
        description="Returns a list of available Telegram commands and features for users",
        arg_model=GetHelpTextArgs,
        handler=get_help_text_tool,
        agents=("main",),
    ),
    ToolSpec(
        name="execute_telegram_command",
        description=(
            "Run an allowed Telegram command as the current user in the current chat. "
            "Only read-only, status, and non-spending self-service commands can be "
            "delegated; commands that transfer or spend coins, handle credentials, or "
            "administer chats must be run by the user directly. The normal command "
            "handler sends its mechanical reply directly, and its events enter the "
            "conversation before the next model response. Returns execution status only."
        ),
        arg_model=ExecuteTelegramCommandArgs,
        handler=execute_telegram_command_tool,
        agents=("main",),
        side_effect=SideEffectKind.COMMAND_DELEGATION,
    ),
    ToolSpec(
        name="read_doc",
        description=(
            "Read an internal reference document about how this bot's own features "
            "work. Omit topic to list every available document with a one-line "
            "summary, then call again with a topic to read that one in full. "
            "Each fact is documented in exactly one topic and the others defer to "
            "it, so follow a cross-reference rather than infer what it omits. "
            "Overly long documents are truncated."
        ),
        arg_model=ReadDocArgs,
        handler=read_doc_tool,
        agents=("main",),
    ),
    ToolSpec(
        name="list_available_stickers",
        description=(
            "List configured Telegram sticker packs, their summaries, and currently "
            "available emoji choices. Use this before adding sticker directives to a reply."
        ),
        arg_model=ListAvailableStickersArgs,
        handler=list_available_stickers_tool,
        agents=("main",),
    ),
    ToolSpec(
        name="google_search",
        description="Use Google search engine to obtain the latest information and answers",
        arg_model=GoogleSearchArgs,
        handler=google_search_tool,
        agents=("main",),
        is_configured=_serpapi_configured,
    ),
    ToolSpec(
        name="advisor",
        description=(
            "Submit one complete reasoning task to a read-only senior advisor. Put "
            "the question or decision in task. Use case_facts only for facts, evidence, "
            "options, and constraints specific to that task. Each call is a single-turn "
            "consultation and cannot receive follow-up messages. The advisor cannot "
            "use tools or take actions. Its response is advisory material for you to "
            "evaluate and synthesize."
        ),
        arg_model=AdvisorArgs,
        handler=advisor_tool,
        agents=("main",),
        is_configured=_advisor_configured,
    ),
    ToolSpec(
        name="fetch_group_context",
        description="Fetch message history from group chat (group chats only)",
        arg_model=FetchGroupContextArgs,
        handler=fetch_group_context_tool,
        agents=("main",),
    ),
    ToolSpec(
        name="fetch_url",
        description=(
            "Fetch a webpage and return extracted main text for up-to-date browsing. "
            "Overly long pages are truncated."
        ),
        arg_model=FetchUrlArgs,
        handler=fetch_url_tool,
        agents=("main",),
    ),
    ToolSpec(
        name="execute_python_code",
        description=(
            "Run Python code remotely and return its output. All results must be "
            "printed with print(), otherwise they will not appear in the output."
        ),
        arg_model=ExecutePythonCodeArgs,
        handler=execute_python_code_tool,
        agents=("main",),
        side_effect=SideEffectKind.EXTERNAL_COMPUTE,
        is_configured=_judge0_configured,
    ),
    ToolSpec(
        name="linux_sandbox",
        description=(
            "Execute a non-interactive shell command in an isolated temporary "
            "Linux sandbox. The sandbox preserves filesystem state across "
            "linux_sandbox calls in the same user request, then closes "
            "automatically. It lives at most 5 minutes and accepts at most 10 "
            "calls per request. Once it closes, the same user cannot open "
            "another sandbox for 5 minutes."
        ),
        arg_model=LinuxSandboxArgs,
        handler=linux_sandbox_tool,
        agents=("main",),
        side_effect=SideEffectKind.EXTERNAL_COMPUTE,
        is_configured=_sandbox_configured,
    ),
    ToolSpec(
        name="generate_image",
        description=(
            "Generate exactly one image from a text prompt. The image is sent to "
            "Telegram immediately after the call succeeds."
        ),
        arg_model=GenerateImageArgs,
        handler=generate_image_tool,
        agents=("main",),
        side_effect=SideEffectKind.TELEGRAM_MEDIA,
        is_configured=_image_gen_configured,
        media=IMAGE_MEDIA_SPEC,
    ),
    ToolSpec(
        name="generate_voice",
        description=(
            "Generate exactly one spoken audio clip from text. The audio is sent to "
            "Telegram immediately after the call succeeds."
        ),
        arg_model=GenerateVoiceArgs,
        handler=generate_voice_tool,
        agents=("main",),
        side_effect=SideEffectKind.TELEGRAM_MEDIA,
        is_configured=_voice_gen_configured,
        media=VOICE_MEDIA_SPEC,
    ),
    ToolSpec(
        name="kindness_gift",
        description=(
            "Gift coins to the user. Each user can receive one gift per 24 hours; "
            "calling again inside that window gifts nothing and returns a cooldown "
            "status. Omit amount to let the system pick one at random."
        ),
        arg_model=KindnessGiftArgs,
        handler=kindness_gift_tool,
        agents=("main",),
        side_effect=SideEffectKind.STATE_MUTATION,
    ),
    ToolSpec(
        name="update_impression",
        description=(
            "Replace the stored impression of the user. This overwrites the previous "
            "impression entirely instead of appending to it, so carry over everything "
            "still worth keeping; the current text is visible as <impression> in the "
            "user profile."
        ),
        arg_model=UpdateImpressionArgs,
        handler=update_impression_tool,
        agents=("main",),
        side_effect=SideEffectKind.STATE_MUTATION,
    ),
    ToolSpec(
        name="fetch_permanent_summaries",
        description="Fetch user's historical conversation summaries (newest on top, max 5 results per request)",
        arg_model=FetchPermanentSummariesArgs,
        handler=fetch_permanent_summaries_tool,
        agents=("main", "recap"),
    ),
    ToolSpec(
        name="search_permanent_records",
        description="Search user's permanent chat snapshots with a regex pattern",
        arg_model=SearchPermanentRecordsArgs,
        handler=search_permanent_records_tool,
        agents=("main", "recap"),
    ),
    ToolSpec(
        name="schedule_ai_message",
        description=(
            "Schedule, list, or cancel one-time or recurring private messages for the user. "
            "Use recurrence parameters when creating recurring schedules. "
            "UTC timestamps only. Max 3 pending tasks, max 12 total (older tasks are overwritten)."
        ),
        arg_model=ScheduleAIMessageArgs,
        handler=schedule_ai_message_tool,
        agents=("main",),
        side_effect=SideEffectKind.STATE_MUTATION,
    ),
    ToolSpec(
        name="user_diary",
        description=(
            "Read or update the internal diary for the current user. For existing "
            "pages, start with index to see compact titles and summaries, then read "
            "relevant pages. Use patch with start_line/end_line to replace those lines; "
            "append adds content at the end. Every content update requires a current "
            "summary, and new or previously untitled pages also require a title. When "
            "a page exceeds its size limit, the oldest content is truncated."
        ),
        arg_model=UserDiaryArgs,
        handler=user_diary_tool,
        agents=("main",),
        side_effect=SideEffectKind.STATE_MUTATION,
    ),
    ToolSpec(
        name="read_diary_page",
        description=(
            "Read one existing diary page selected from diary_index. This tool is "
            "read-only and cannot create, edit, or delete diary pages."
        ),
        arg_model=ReadDiaryPageArgs,
        handler=read_diary_page_tool,
        agents=("recap",),
    ),
    ToolSpec(
        name="search_prior_context",
        description=(
            "Search up to 20 earlier archived summaries with lightweight BM25 "
            "ranking and a small recency boost. Use concrete terms from the current "
            "snapshot to resolve older references or continuity."
        ),
        arg_model=SummarySearchPriorContextArgs,
        handler=search_prior_context_tool,
        agents=("summary",),
    ),
)

TOOL_SPEC_BY_NAME: Mapping[str, ToolSpec] = MappingProxyType(
    {spec.name: spec for spec in TOOL_SPECS}
)
if len(TOOL_SPEC_BY_NAME) != len(TOOL_SPECS):
    raise RuntimeError("Duplicate tool name in TOOL_SPECS")

AI_TOOL_ARG_MODELS.clear()
AI_TOOL_ARG_MODELS.update({spec.name: spec.arg_model for spec in TOOL_SPECS})

_MEDIA_TOOL_SPECS_ORDERED: tuple[tuple[str, MediaToolSpec], ...] = tuple(
    sorted(
        ((spec.name, spec.media) for spec in TOOL_SPECS if spec.media is not None),
        key=lambda item: item[1].batch_order,
    )
)


def get_tool_spec(name: str) -> ToolSpec | None:
    return TOOL_SPEC_BY_NAME.get(name)


def get_media_tool_spec(name: str) -> MediaToolSpec | None:
    spec = TOOL_SPEC_BY_NAME.get(name)
    return spec.media if spec is not None else None


def iter_media_tool_specs() -> tuple[tuple[str, MediaToolSpec], ...]:
    return _MEDIA_TOOL_SPECS_ORDERED


def agent_tool_schemas(agent: str) -> list[dict[str, Any]]:
    """返回属于 `agent` 的全部静态工具 schema（不做运行时可用性过滤，供文档目录与默认全集使用）。"""
    return [spec.schema() for spec in TOOL_SPECS if agent in spec.agents]


def agent_tool_handlers(agent: str) -> dict[str, Callable[..., Any]]:
    """返回属于 `agent` 的工具处理函数映射。"""
    return {spec.name: spec.handler for spec in TOOL_SPECS if agent in spec.agents}


MAIN_TOOL_HANDLERS: dict[str, Callable[..., Any]] = agent_tool_handlers("main")


def resolve_agent_tools(
    agent: str,
    *,
    provider: ai_providers.ProviderSpec | None = None,
    settings: Any = None,
) -> list[dict[str, Any]]:
    """返回 `agent` 暴露给模型的工具 schema：去掉没配置凭据的工具和 provider 声明不支持的工具。

    配置在进程运行期间不变，所以同一个 provider 拿到的列表每一轮都相同。
    """
    skipped = frozenset(provider.skip_tools) if provider is not None else frozenset()
    return [
        spec.schema()
        for spec in TOOL_SPECS
        if agent in spec.agents
        and spec.name not in skipped
        and spec.is_configured(settings)
    ]


__all__ = [
    "IMAGE_MEDIA_SPEC",
    "MAIN_TOOL_HANDLERS",
    "MediaToolSpec",
    "SideEffectKind",
    "TOOL_SPECS",
    "TOOL_SPEC_BY_NAME",
    "ToolSpec",
    "VOICE_MEDIA_SPEC",
    "agent_tool_handlers",
    "agent_tool_schemas",
    "get_media_tool_spec",
    "get_tool_spec",
    "iter_media_tool_specs",
    "resolve_agent_tools",
]
