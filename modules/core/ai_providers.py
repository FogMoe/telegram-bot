"""AI provider 的唯一声明表。

每个 provider 的名字、别名、各任务的模型配置键、凭据与 base URL 配置、能力、需要跳过的工具，
以及它特有的协议要求都只在这里声明一次。`litellm_models`（模型名）、
`features/ai/litellm_provider_config`（认证参数）、`features/ai/provider_resolver`（任务到
provider/模型的解析）、`features/ai/litellm_message_sanitizer`（历史消息协议差异）、
`features/ai/chat_provider`（聊天入口）与 `core/chat_records`（token 计数用的模型）都从这里读取，
不再各自维护一份 provider 表。契约说明见 docs/ai-provider-architecture.md。

声明只描述 provider，不做网络调用，也不 import features，所以放在 core。

读取配置时的来源参数 `settings` 是任何以 `core.config` 里的名字作为属性的对象：默认是
`core.config` 模块本身（调用时才读，能看到 `config.install_settings` 与 monkeypatch），
单元测试可以传一个 `SimpleNamespace` 来脱离全局配置。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from . import config

# 业务任务名。`chat` 是主对话，其余是后台任务；每个任务在 `TASK_SPECS` 里声明 provider 配置前缀。
CHAT_TASK = "chat"

# LiteLLM 能识别的 provider 前缀：模型名已经带有这些前缀时不再重复添加。
LITELLM_PREFIXES = ("openai/", "openrouter/", "azure/", "gemini/", "zai/")


class BaseUrlStyle(StrEnum):
    """配置里的 base URL 在交给 LiteLLM 之前的整理方式。"""

    VERBATIM = "verbatim"  # 原样使用，为空则不传
    OPENAI_COMPATIBLE = "openai_compatible"  # 去掉末尾的 /chat/completions
    GEMINI = "gemini"  # 兼容模式同上；原生模式去掉末尾的 /models
    AZURE = "azure"  # endpoint 优先，否则从 deployment 形式的 base URL 截出资源根


class ModelPrefixRule(StrEnum):
    """把配置里的模型名变成 LiteLLM 模型名的规则。"""

    KNOWN_PREFIXES = "known_prefixes"  # 已带任一 LiteLLM 前缀的原样使用
    OWN_PREFIX = "own_prefix"  # 只认自己的前缀（OpenRouter 的模型 id 本身以 openai/ 等开头）
    ALWAYS = "always"  # 始终添加前缀（FOGMOE 端点的模型 id 本身可能以 openai/ 开头）


@dataclass(frozen=True, slots=True)
class Credentials:
    """provider 的认证与端点配置，值是 `core.config` 里的名字。"""

    api_key: str
    api_base: str | None = None
    base_style: BaseUrlStyle = BaseUrlStyle.VERBATIM
    base_required: bool = False
    # 自定义 base URL 时允许没有 key，用占位 key 代替（OpenAI-compatible 的本地端点）。
    keyless_with_base: bool = False
    # api_base 为空时的后备配置（Azure 的 deployment 形式 base URL）。
    api_base_fallback: str | None = None
    api_version: str | None = None


@dataclass(frozen=True, slots=True)
class Capabilities:
    """provider 级别的能力。模型级别的限制（例如纯文本模型）见 `AI_CHAT_TEXT_ONLY_MODELS`。"""

    tools: bool = True
    vision: bool = True


@dataclass(frozen=True, slots=True)
class WireProtocol:
    """历史消息发给 provider 之前必须满足的协议差异。默认是标准 OpenAI chat 格式。"""

    # 保留 provider_specific_fields（Gemini 原生协议靠它回传 thought signature）。
    keeps_provider_specific_fields: bool = False
    # 去掉 tool call 的 id 与 tool 消息的 tool_call_id（Gemini 原生协议按位置配对）。
    strips_tool_call_ids: bool = False
    # 带 tool_calls 的 assistant 消息内容为空白时去掉 content 字段。
    omits_blank_content_on_tool_calls: bool = False
    # 自定义的原生端点只认驼峰的 systemInstruction，请求体里的 system_instruction 要改名。
    camel_case_system_instruction: bool = False


OPENAI_WIRE = WireProtocol()
GEMINI_NATIVE_WIRE = WireProtocol(
    keeps_provider_specific_fields=True,
    strips_tool_call_ids=True,
    omits_blank_content_on_tool_calls=True,
    camel_case_system_instruction=True,
)


def _no_mapping() -> Mapping[str, str]:
    return MappingProxyType({})


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    name: str  # 规范名，也是传给 LiteLLM 层和日志的名字
    display_name: str  # 日志与工具循环里的展示名
    credentials: Credentials
    # 模型配置键 `<model_prefix>_<TASK>_MODEL`，例如 OPENAI_CHAT_MODEL。
    model_prefix: str
    litellm_prefix: str
    prefix_rule: ModelPrefixRule = ModelPrefixRule.KNOWN_PREFIXES
    aliases: tuple[str, ...] = ()
    # 任务 -> 备用模型的配置键，主模型失败后依次尝试。
    fallback_models: Mapping[str, str] = field(default_factory=_no_mapping)
    capabilities: Capabilities = Capabilities()
    wire_protocol: WireProtocol = OPENAI_WIRE
    # 配置里为真时，该 provider 走 OpenAI-compatible 端点：模型名、历史消息协议都按 OpenAI 处理。
    openai_compatible_flag: str | None = None
    # 主聊天里要对该 provider 隐藏的工具。
    skip_tools: tuple[str, ...] = ()
    # 响应被安全策略拦截时：把错误文本识别为 SafetyBlockError / 让路由换下一个 provider。
    translates_safety_blocks: bool = False
    safety_blocks_fall_through: bool = False
    # 兼容 BerriAI/litellm#35444：文本与工具调用被拆到不同 choices，需要合并读取。
    merges_split_tool_call_choices: bool = False

    def model_key(self, task: str) -> str:
        return f"{self.model_prefix}_{task.upper()}_MODEL"

    def fallback_model_key(self, task: str) -> str | None:
        return self.fallback_models.get(task.lower())

    def uses_openai_compatible_endpoint(self, settings: Any = None) -> bool:
        if not self.openai_compatible_flag:
            return False
        return bool(_read(settings, self.openai_compatible_flag))

    def wire_protocol_for(self, settings: Any = None) -> WireProtocol:
        if self.uses_openai_compatible_endpoint(settings):
            return OPENAI_WIRE
        return self.wire_protocol

    def litellm_provider(self, settings: Any = None) -> str:
        if self.uses_openai_compatible_endpoint(settings):
            return "openai"
        return self.litellm_prefix

    def supports(self, capability: str) -> bool:
        return bool(getattr(self.capabilities, capability))


@dataclass(frozen=True, slots=True)
class TaskSpec:
    name: str
    # `<prefix>_PROVIDER` 与 `<prefix>_FALLBACK_PROVIDER` 决定该任务的 provider 顺序；
    # chat 没有前缀，顺序来自 AI_CHAT_ORDER。
    provider_config_prefix: str | None = None
    # provider 必须具备的能力，缺少时该任务跳过它。
    requires: tuple[str, ...] = ()


TASK_SPECS: Mapping[str, TaskSpec] = MappingProxyType(
    {
        spec.name: spec
        for spec in (
            TaskSpec("chat", requires=("tools",)),
            TaskSpec("recap", "AI_RECAP"),
            TaskSpec("summary", "AI_SUMMARY"),
            TaskSpec("translate", "AI_TRANSLATE"),
            TaskSpec("vision", "AI_VISION", requires=("vision",)),
            TaskSpec("classifier", "AI_CLASSIFIER"),
            TaskSpec("advisor", "AI_ADVISOR"),
        )
    }
)


PROVIDERS: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        name="openai",
        display_name="OpenAI",
        credentials=Credentials(
            api_key="OPENAI_API_KEY",
            api_base="OPENAI_BASE_URL",
            keyless_with_base=True,
        ),
        model_prefix="OPENAI",
        litellm_prefix="openai",
    ),
    ProviderSpec(
        name="openrouter",
        display_name="OpenRouter",
        credentials=Credentials(
            api_key="OPENROUTER_API_KEY",
            api_base="OPENROUTER_API_BASE",
            base_style=BaseUrlStyle.OPENAI_COMPATIBLE,
            base_required=True,
        ),
        model_prefix="OPENROUTER",
        litellm_prefix="openrouter",
        prefix_rule=ModelPrefixRule.OWN_PREFIX,
    ),
    ProviderSpec(
        name="fogmoe",
        display_name="FOGMOE",
        credentials=Credentials(
            api_key="FOGMOE_API_KEY",
            api_base="FOGMOE_API_BASE",
            base_style=BaseUrlStyle.OPENAI_COMPATIBLE,
            base_required=True,
        ),
        model_prefix="FOGMOE",
        litellm_prefix="openai",
        prefix_rule=ModelPrefixRule.ALWAYS,
        merges_split_tool_call_choices=True,
    ),
    ProviderSpec(
        name="gemini",
        display_name="Gemini",
        credentials=Credentials(
            api_key="GEMINI_API_KEY",
            api_base="GEMINI_API_BASE",
            base_style=BaseUrlStyle.GEMINI,
        ),
        model_prefix="GEMINI",
        litellm_prefix="gemini",
        fallback_models=MappingProxyType(
            {
                "chat": "GEMINI_CHAT_FALLBACK_MODEL",
                "summary": "GEMINI_SUMMARY_FALLBACK_MODEL",
            }
        ),
        wire_protocol=GEMINI_NATIVE_WIRE,
        openai_compatible_flag="GEMINI_OPENAI_COMPATIBLE",
        translates_safety_blocks=True,
        safety_blocks_fall_through=True,
    ),
    ProviderSpec(
        name="azure",
        display_name="Azure",
        credentials=Credentials(
            api_key="AZURE_OPENAI_API_KEY",
            api_base="AZURE_OPENAI_API_ENDPOINT",
            api_base_fallback="AZURE_OPENAI_BASE_URL",
            base_style=BaseUrlStyle.AZURE,
            base_required=True,
            api_version="AZURE_OPENAI_API_VERSION",
        ),
        model_prefix="AZURE_OPENAI",
        litellm_prefix="azure",
    ),
    ProviderSpec(
        name="siliconflow",
        display_name="SiliconFlow",
        credentials=Credentials(
            api_key="SILICONFLOW_API_KEY",
            api_base="SILICONFLOW_API_BASE",
            base_style=BaseUrlStyle.OPENAI_COMPATIBLE,
            base_required=True,
        ),
        model_prefix="SILICONFLOW",
        litellm_prefix="openai",
    ),
    ProviderSpec(
        name="zai",
        display_name="Z.ai",
        aliases=("zhipu",),
        credentials=Credentials(api_key="ZAI_API_KEY", api_base="ZAI_API_BASE"),
        model_prefix="ZHIPU",
        litellm_prefix="zai",
        skip_tools=("web_search", "web_browser"),
    ),
)

_BY_NAME: Mapping[str, ProviderSpec] = MappingProxyType(
    {
        key: spec
        for spec in PROVIDERS
        for key in (spec.name, *spec.aliases)
    }
)


def _read(settings: Any, key: str) -> Any:
    source = config if settings is None else settings
    return getattr(source, key, None)


def read_setting(settings: Any, key: str) -> Any:
    """按名字读配置；`settings` 为 None 时读 `core.config`。"""
    return _read(settings, key)


def lookup(name: str | None) -> ProviderSpec | None:
    """按名字或别名（忽略大小写与首尾空白）查找 provider，找不到返回 None。"""
    return _BY_NAME.get((name or "").strip().lower())


def require(name: str | None) -> ProviderSpec:
    spec = lookup(name)
    if spec is None:
        raise RuntimeError(f"Unsupported AI provider: {name}")
    return spec


def configured_model(spec: ProviderSpec, task: str, settings: Any = None) -> str | None:
    return _read(settings, spec.model_key(task))


def configured_fallback_model(
    spec: ProviderSpec,
    task: str,
    settings: Any = None,
) -> str | None:
    key = spec.fallback_model_key(task)
    return _read(settings, key) if key else None


def configured_models(name: str, task: str, settings: Any = None) -> list[str | None]:
    """某个 provider 在某任务上配置的模型（主模型在前，备用在后），原样返回、不去重。

    未知 provider 返回空列表。
    """
    spec = lookup(name)
    if spec is None:
        return []
    models = [configured_model(spec, task, settings)]
    if spec.fallback_model_key(task):
        models.append(configured_fallback_model(spec, task, settings))
    return models


def litellm_model_name(provider: str, model: str | None, settings: Any = None) -> str:
    spec = require(provider)
    if not model:
        raise RuntimeError(f"Missing model configuration for provider: {spec.name}")
    prefix = spec.litellm_provider(settings)
    if spec.prefix_rule is ModelPrefixRule.ALWAYS:
        return f"{prefix}/{model}"
    if spec.prefix_rule is ModelPrefixRule.OWN_PREFIX:
        return model if model.startswith(f"{prefix}/") else f"{prefix}/{model}"
    return model if model.startswith(LITELLM_PREFIXES) else f"{prefix}/{model}"
