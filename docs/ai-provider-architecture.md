# AI Provider 架构重构设计

## 背景

当前项目已经支持多个 AI 服务，包括 Gemini、Z.ai 和 Azure OpenAI。主聊天路径通过 `router.py` 根据 `AI_SERVICE_ORDER` 依次尝试不同 provider，但 summary、翻译、图片分析、群聊触发判断等子模块仍然直接绑定某个具体 provider。

这导致一个实际问题：如果希望把 OpenAI 作为主 provider，并逐步放弃 Gemini，不能只改一个主聊天配置。summary 仍然硬编码 Gemini，翻译、vision、classifier 仍然硬编码 Z.ai。每次切换 provider 都需要逐个追踪业务模块，容易遗漏，也容易把 provider 差异扩散到业务代码里。

## 目标

1. 让 provider 选择集中配置，而不是散落在不同业务模块。
2. 允许按任务配置 provider，例如聊天、summary、翻译、vision、classifier 分别选择不同 provider。
3. 支持 fallback，例如 summary 优先用 OpenAI，失败后回退 Azure。
4. 保留各 provider 的差异化实现，不强行假设所有 provider 都支持同一组参数。
5. 让业务模块只表达任务需求，不关心 OpenAI、Gemini、Azure、Z.ai 的 SDK 参数差异。
6. 添加 OpenAI provider 时不破坏现有 Gemini、Z.ai、Azure 行为。
7. 以后切换主 provider 时尽量只改 `.env`，不改业务代码。

## 当前痛点

### 主聊天和子模块的 provider 策略不一致

主聊天路径已经有统一 router：

```python
AI_SERVICE_ORDER = ["gemini", "zhipu", "azure"]
```

但其他路径没有复用这个机制：

- summary 直接调用 `create_gemini_client()`
- 翻译直接调用 `create_zhipu_client()`
- 图片分析直接调用 `create_zhipu_client()`
- 群聊是否触发回复直接调用 `create_zhipu_client()`

因此主聊天切换到 OpenAI 后，其他 AI 功能仍然会继续请求旧 provider。

### Provider 参数并不完全兼容

虽然这些服务大多提供 OpenAI-compatible API，但兼容程度并不完全一致。例如：

- 某些 provider 不支持 `tool_choice`
- 某些 provider 对工具调用格式支持不完整
- 某些 provider 的 vision message 格式或能力不同
- Azure 需要 `api-version` query 和 `api-key` header
- Gemini 可能对部分 OpenAI 参数支持有限
- 不同 provider 的模型名、部署名、base_url 语义不同

如果把这些差异直接写进 summary、translate、vision 等业务模块，代码会迅速变成到处都是 `if provider == "xxx"`。

### 模型配置缺少任务维度

当前模型配置主要按 provider 命名，例如：

```env
GEMINI_MODEL=...
ZHIPU_MODEL=...
AZURE_OPENAI_MODEL=...
SUMMARY_MODEL=...
```

这无法清楚表达“同一个 provider 在不同任务中使用不同模型”。例如 OpenAI 可以用：

- 聊天：`gpt-4.1`
- summary：`gpt-4.1-mini`
- 翻译：`gpt-4.1-mini`
- vision：`gpt-4.1`
- classifier：`gpt-4.1-nano`

如果没有任务维度，后续配置会越来越混乱。

## 解决方案概览

引入三层结构：

1. Provider Adapter：每个 provider 一个 adapter，负责处理自己的 SDK 参数和能力差异。
2. Task Resolver：根据任务名选择 provider、model 和 fallback。
3. Business Task：summary、translate、vision、classifier 等业务模块只描述任务请求，不直接依赖具体 provider。

目标调用形态：

```python
adapter = get_ai_adapter_for_task("summary")
response = adapter.create_completion(AIRequest(
    task="summary",
    messages=messages,
    temperature=0.2,
    max_tokens=2500,
))
```

业务模块不再关心底层是 OpenAI、Azure、Gemini 还是 Z.ai。

## Provider Adapter

Provider adapter 是每个 provider 的唯一适配层。它负责：

- 创建 client
- 选择模型
- 映射参数
- 删除不支持的参数
- 处理 provider 特有 header、query、base_url
- 声明能力
- 统一返回结果接口

建议定义通用请求对象：

```python
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class AIRequest:
    task: str
    messages: List[Dict[str, Any]]
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[str | Dict[str, object]] = None
```

建议定义 provider 能力：

```python
@dataclass
class ProviderCapabilities:
    chat: bool = True
    tools: bool = False
    vision: bool = False
    tool_choice: bool = False
    system_messages: bool = True
```

Adapter 接口示意：

```python
class BaseAIAdapter:
    name: str
    capabilities: ProviderCapabilities

    def create_completion(self, request: AIRequest):
        raise NotImplementedError

    def model_for(self, task: str) -> str:
        raise NotImplementedError
```

### OpenAI Adapter

OpenAI adapter 使用官方 OpenAI SDK 默认参数。它不需要 Azure 的 `api-version`，也不需要 Azure 的 `api-key` header。

示意：

```python
class OpenAIAdapter(BaseAIAdapter):
    name = "openai"
    capabilities = ProviderCapabilities(
        chat=True,
        tools=True,
        vision=True,
        tool_choice=True,
        system_messages=True,
    )

    def create_completion(self, request: AIRequest):
        kwargs = {
            "model": self.model_for(request.task),
            "messages": request.messages,
        }
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if request.max_tokens is not None:
            kwargs["max_tokens"] = request.max_tokens
        if request.tools:
            kwargs["tools"] = request.tools
        if request.tool_choice is not None and self.capabilities.tool_choice:
            kwargs["tool_choice"] = request.tool_choice

        return self.client.chat.completions.create(**kwargs)
```

### Azure Adapter

Azure adapter 行为可以接近 OpenAI adapter，但 client 创建不同：

- 使用 `AZURE_OPENAI_API_KEY`
- 使用 `AZURE_OPENAI_BASE_URL`
- 使用 `default_headers={"api-key": ...}`
- 使用 `default_query={"api-version": ...}`
- 模型参数通常传 deployment/model 配置

Azure 的 provider 差异应留在 adapter 内部，不让业务模块感知。

### Gemini Adapter

Gemini 当前通过 OpenAI-compatible endpoint 接入，但不能假设它完整支持 OpenAI 全部参数。

Gemini adapter 应负责：

- 只传 Gemini 当前支持的参数
- 对 safety block 做 provider 内部错误归一
- 如果不支持某些工具参数，adapter 内部删除或降级
- 使用 Gemini 自己的主模型和 fallback 模型

### Z.ai Adapter

Z.ai adapter 负责保留当前行为：

- 聊天使用 `ZHIPU_MODEL`
- 翻译使用 `ZHIPU_TRANSLATE_MODEL`
- vision 使用 `ZHIPU_VISION_MODEL`
- 可继续对部分工具做 skip，例如当前主聊天对 Z.ai 跳过 `web_search` 和 `web_browser`

这些差异不应进入 summary、translate、vision 等业务模块。

## Task Resolver

Task Resolver 负责把任务映射到 provider 和 model。

建议支持这些任务：

- `chat`
- `summary`
- `translate`
- `vision`
- `classifier`

示意接口：

```python
def get_provider_order_for_task(task: str) -> list[str]:
    ...


def get_adapter_for_task(task: str):
    ...


def get_model_for_task(provider: str, task: str) -> str:
    ...
```

对于主聊天，resolver 返回 provider 顺序。对于 summary、translate、vision、classifier，resolver 可以返回一个主 provider 加 fallback provider。

## 推荐配置

建议将配置从“只按 provider”扩展为“provider + task”。

```env
# 全局默认 provider
AI_DEFAULT_PROVIDER=openai

# 主聊天 fallback 顺序
AI_CHAT_ORDER=openai,azure,zhipu,gemini

# 子任务 provider
AI_SUMMARY_PROVIDER=openai
AI_SUMMARY_FALLBACK_PROVIDER=azure
AI_TRANSLATE_PROVIDER=openai
AI_TRANSLATE_FALLBACK_PROVIDER=zhipu
AI_VISION_PROVIDER=openai
AI_VISION_FALLBACK_PROVIDER=zhipu
AI_CLASSIFIER_PROVIDER=openai
AI_CLASSIFIER_FALLBACK_PROVIDER=zhipu
```

OpenAI 配置：

```env
OPENAI_API_KEY=...
OPENAI_BASE_URL=
OPENAI_CHAT_MODEL=gpt-4.1
OPENAI_SUMMARY_MODEL=gpt-4.1-mini
OPENAI_TRANSLATE_MODEL=gpt-4.1-mini
OPENAI_VISION_MODEL=gpt-4.1
OPENAI_CLASSIFIER_MODEL=gpt-4.1-nano
```

Azure 配置可以保留当前变量，并逐步补齐任务级模型：

```env
AZURE_OPENAI_API_ENDPOINT=https://your-resource-name.openai.azure.com/
AZURE_OPENAI_API_KEY=...
AZURE_OPENAI_API_VERSION=2024-12-01-preview
AZURE_OPENAI_DEPLOYMENT=...
AZURE_OPENAI_MODEL=...

AZURE_OPENAI_CHAT_MODEL=...
AZURE_OPENAI_SUMMARY_MODEL=...
AZURE_OPENAI_TRANSLATE_MODEL=...
AZURE_OPENAI_VISION_MODEL=...
AZURE_OPENAI_CLASSIFIER_MODEL=...
```

Gemini 和 Z.ai 保留现有配置，同时允许新增任务级覆盖：

```env
GEMINI_CHAT_MODEL=...
GEMINI_SUMMARY_MODEL=...

ZHIPU_CHAT_MODEL=...
ZHIPU_TRANSLATE_MODEL=...
ZHIPU_VISION_MODEL=...
ZHIPU_CLASSIFIER_MODEL=...
```

兼容策略：

- 如果任务级模型不存在，回退到 provider 默认模型。
- 如果任务级 provider 不存在，回退到 `AI_DEFAULT_PROVIDER`。
- 如果 fallback provider 不存在，该任务只尝试主 provider。

## 业务模块改造方式

### Summary

当前 summary 直接创建 Gemini client。改造后：

```python
response = run_ai_task(
    "summary",
    messages=messages,
    temperature=0.2,
    max_tokens=SUMMARY_MAX_TOKENS,
)
```

summary 模块只负责生成 prompt、保存摘要，不关心 provider。

### Translate

当前翻译直接创建 Z.ai client。改造后：

```python
response = run_ai_task(
    "translate",
    messages=messages,
)
```

限流逻辑可以继续保留在 translate 模块里，provider 选择交给 resolver。

### Vision

当前 vision 直接创建 Z.ai client。改造后：

```python
response = run_ai_task(
    "vision",
    messages=messages,
)
```

如果某 provider 不支持 vision，resolver 应根据 capability 跳过该 provider 并尝试 fallback。

### Classifier

群聊是否触发回复目前硬编码 Z.ai。改造后：

```python
response = run_ai_task(
    "classifier",
    messages=messages,
    temperature=0.0,
)
```

这样 OpenAI 可以使用更便宜更快的 classifier 模型，Z.ai 也可以作为 fallback。

### Chat Tool Loop

当前 `run_tool_loop` 直接接收 OpenAI SDK client 并调用：

```python
client.chat.completions.create(...)
```

改造后应接收 adapter：

```python
adapter.create_completion(AIRequest(
    task="chat",
    messages=filtered_messages,
    tools=tools,
    tool_choice=request_tool_choice,
    temperature=temperature,
    max_tokens=max_tokens,
))
```

这样 `run_tool_loop` 不再需要知道 provider 的 SDK 参数差异。

## Fallback 策略

建议按任务独立 fallback。

主聊天：

```env
AI_CHAT_ORDER=openai,azure,zhipu,gemini
```

summary：

```env
AI_SUMMARY_PROVIDER=openai
AI_SUMMARY_FALLBACK_PROVIDER=azure
```

vision：

```env
AI_VISION_PROVIDER=openai
AI_VISION_FALLBACK_PROVIDER=zhipu
```

执行逻辑：

1. 读取任务 provider 顺序。
2. 检查 provider 是否配置完整。
3. 检查 provider 是否支持该任务所需能力。
4. 调用 adapter。
5. 如果失败，记录日志并尝试下一个 provider。
6. 所有 provider 都失败后，返回当前业务模块定义的用户友好错误。

## 文件结构建议

建议新增或调整以下文件：

```text
modules/features/ai/
  adapters/
    __init__.py
    base.py
    openai.py
    azure.py
    gemini.py
    zhipu.py
  task_runner.py
  provider_resolver.py
```

职责：

- `adapters/base.py`：定义 `AIRequest`、`ProviderCapabilities`、`BaseAIAdapter`
- `adapters/openai.py`：OpenAI adapter
- `adapters/azure.py`：Azure adapter
- `adapters/gemini.py`：Gemini adapter
- `adapters/zhipu.py`：Z.ai adapter
- `provider_resolver.py`：解析 task 到 provider/model/fallback
- `task_runner.py`：提供 `run_ai_task()`，封装 fallback 执行

现有 `providers/` 可以短期保留，迁移完成后再决定是否删除或合并。

## 迁移步骤

### 第一阶段：加 OpenAI，但不改现有行为

1. 增加 OpenAI 配置。
2. 增加 `create_openai_client()`。
3. 增加 OpenAI provider 或 adapter。
4. 将 OpenAI 注册到主聊天 router。
5. 默认 `AI_CHAT_ORDER` 仍保持旧顺序，避免上线即改变行为。

### 第二阶段：引入 adapter 和 resolver

1. 新增 adapter 基类和 OpenAI/Azure/Gemini/Z.ai adapter。
2. 新增 task resolver。
3. 新增 `run_ai_task()`。
4. 保持现有 provider 文件可用，降低一次性改动风险。

### 第三阶段：迁移子模块

按风险从低到高迁移：

1. classifier
2. translate
3. summary
4. vision
5. chat tool loop

每迁移一个模块，都保持原有默认 provider 不变，先验证行为一致。

### 第四阶段：切换默认 provider

当 adapter 路径稳定后，再通过 `.env` 切换：

```env
AI_DEFAULT_PROVIDER=openai
AI_CHAT_ORDER=openai,azure
AI_SUMMARY_PROVIDER=openai
AI_TRANSLATE_PROVIDER=openai
AI_VISION_PROVIDER=openai
AI_CLASSIFIER_PROVIDER=openai
```

如果希望完全放弃 Gemini，则可以移除 Gemini key，或者把 Gemini 从所有 provider order 中删除。

## 风险和注意事项

### 工具调用兼容性

不同 provider 的 tool calling 支持程度不同。主聊天如果依赖工具调用，resolver 必须优先选择 `capabilities.tools = True` 的 provider。

如果某 provider 不支持某些工具参数，adapter 应在内部降级，而不是让业务模块处理。

### Vision 兼容性

Vision 输入格式可能存在差异。vision task 应明确要求 `capabilities.vision = True`。不支持 vision 的 provider 应直接跳过。

### 模型名和部署名

Azure 的 deployment 和 model 语义容易混淆。adapter 内部应统一处理，不应让业务模块判断 Azure 应该传 deployment 还是 model。

### 错误处理

不同 provider 的异常格式不同。adapter 或 task runner 应统一记录：

- provider 名称
- task 名称
- model 名称
- 错误摘要

业务模块只负责返回用户可见错误文本。

### 默认行为兼容

迁移期间不要立即改变现有默认 provider：

- summary 仍默认 Gemini
- translate 默认 Z.ai
- vision 默认 Z.ai
- classifier 默认 Z.ai
- chat 默认 `["gemini", "zhipu", "azure"]`

等 adapter 稳定后，再通过配置切换到 OpenAI。

## 最终效果

完成后，切换 OpenAI 为主 provider 不再需要修改多个业务模块。只需要调整配置：

```env
AI_DEFAULT_PROVIDER=openai
AI_CHAT_ORDER=openai,azure
AI_SUMMARY_PROVIDER=openai
AI_TRANSLATE_PROVIDER=openai
AI_VISION_PROVIDER=openai
AI_CLASSIFIER_PROVIDER=openai
```

Provider 差异集中在 adapter 中；业务模块只描述任务本身。这样既能支持 OpenAI 作为主 provider，也能继续保留 Azure、Z.ai、Gemini 作为 fallback 或特定任务 provider。
