# AI Provider 契约

本页是 AI provider 的契约：provider 在哪里声明、任务如何解析到 provider 与模型、主聊天如何 fallback、
provider 特有的协议要求由什么承载。权威定义是代码，本页只讲规则并指向符号，不复述字段值。
环境变量以 [`.env.example`](../.env.example) 为准。

## 单一声明表

所有 provider 只在 `modules/core/ai_providers.py` 的 `PROVIDERS` 里声明一次，任务在同一文件的
`TASK_SPECS` 里声明。其他代码都从这里读取，不再各自维护 provider 表：

| 用途 | 入口 | 从声明表读取 |
|---|---|---|
| provider 名字与别名解析 | `ai_providers.lookup` / `require`、`core/litellm_models.py` 的 `normalize_provider` | `name`、`aliases` |
| LiteLLM 模型名 | `ai_providers.litellm_model_name`（`core/litellm_models.py` 转发） | `litellm_prefix`、`prefix_rule`、`openai_compatible_flag` |
| 认证与端点参数 | `features/ai/litellm_provider_config.py` 的 `provider_params` | `credentials` |
| 任务的 provider 顺序与模型 | `features/ai/provider_resolver.py` | `TASK_SPECS`、`model_prefix`、`fallback_models` |
| 后台任务执行与回退 | `features/ai/task_runner.py` 的 `run_ai_task` | `TaskSpec.requires`、`capabilities` |
| 主聊天的 provider 入口 | `features/ai/chat_provider.py` 的 `run_chat_provider` | 模型、`display_name`、`skip_tools`、`translates_safety_blocks`、`capabilities` |
| 主聊天路由、熔断与图片降级 | `features/ai/router.py` | `safety_blocks_fall_through`、视觉能力 |
| 历史消息的协议整理 | `features/ai/litellm_message_sanitizer.py` | `wire_protocol` |
| 工具循环里的协议兼容 | `features/ai/tool_runner.py` 的 `_resolve_assistant_message` | `merges_split_tool_call_choices` |
| 聊天模型是否支持图片 | `features/ai/chat_capabilities.py` | `capabilities`、`AI_CHAT_TEXT_ONLY_MODELS` |
| token 计数使用的模型 | `core/chat_records.py` 的 `_chat_token_count_model` | `configured_models` |

声明里引用的每个配置名都必须是 `core/config.py` 的 `AppSettings` 字段，
`tests/test_ai_provider_registry.py` 逐项检查。

声明表、任务解析、认证参数与 `run_chat_provider` 都有可选的 `settings` 参数，默认读 `core.config` 的当前值；
单元测试可以传一个只含所需名字的对象，见 [architecture.md](architecture.md) 的「配置注入」。

## 声明的内容

`ProviderSpec` 的字段分四组，字段含义以 `ai_providers.py` 里的注释为准：

- **标识**：规范名 `name`、别名 `aliases`（`zhipu` 解析到 `zai`）、日志与工具循环使用的 `display_name`。
  名字比较忽略大小写与首尾空白。
- **模型与凭据**：`model_prefix` 生成每个任务的模型配置键 `<model_prefix>_<TASK>_MODEL`；
  `fallback_models` 给出个别任务的备用模型配置键；`credentials` 描述 key、base URL 的配置名与整理方式
  （`BaseUrlStyle`）、哪些必填。缺少必填项时 `provider_params` 抛出 `Missing <配置名> configuration.`。
- **能力**：`capabilities`（工具、视觉）是 provider 级别的声明。模型级别的限制另由
  `AI_CHAT_TEXT_ONLY_MODELS`（模型名通配）表达。任务通过 `TaskSpec.requires` 声明需要的能力：
  chat 需要工具，vision 需要视觉。
- **协议与行为**：下一节。

## provider 特有的协议要求

这些差异只由声明承载，业务模块与路由里没有 `if provider == ...`：

- **历史消息协议（`wire_protocol`）**：发给 provider 之前，`litellm_message_sanitizer` 按 `WireProtocol`
  的三项开关整理消息：是否保留 `provider_specific_fields`、是否去掉 tool call 的 id 与 tool 消息的
  `tool_call_id`、带 `tool_calls` 且内容为空白的 assistant 消息是否去掉 `content`。
  `camel_case_system_instruction` 让 `litellm_client` 在自定义原生端点上把 `system_instruction` 改名为
  `systemInstruction`。
- **原生与 OpenAI-compatible 两种端点（`openai_compatible_flag`）**：配置里的开关为真时，该 provider
  的模型名前缀、base URL 整理与历史消息协议都按 OpenAI 处理；为假时用声明的原生协议。
  切换由 `ProviderSpec.wire_protocol_for` 与 `litellm_provider` 统一给出，调用方不自行判断。
- **备用模型（`fallback_models`）**：`run_chat_provider` 在主模型失败后尝试备用模型；
  上下文超限与工具执行后的部分失败（`PartialAIResponseError`）不换模型，原样上抛。
- **安全拦截**：`translates_safety_blocks` 让 `run_chat_provider` 在没有可用备用模型时，把「被安全策略拦截」的
  错误转成 `SafetyBlockError`；`safety_blocks_fall_through` 让 router 遇到它时换下一个 provider，
  没有声明的 provider 遇到它直接抛出。已经向用户发送过可见内容时都不再重试。
- **隐藏工具（`skip_tools`）**：主聊天里对该 provider 不暴露的工具名。
- **拆分的 choices（`merges_split_tool_call_choices`）**：某些端点把文本与工具调用拆到不同 choices，
  工具循环保留 choice 0 的文本并读取后续 choice 的工具调用。这是对上游缺陷
  [BerriAI/litellm#35444](https://github.com/BerriAI/litellm/issues/35444) 的临时兼容。
- **模型名规则（`prefix_rule`）**：是否承认已有的 LiteLLM 前缀，以及是否始终添加前缀。

## 任务解析

`provider_resolver.get_provider_order_for_task(task)`：

- `chat` 的顺序来自 `AI_CHAT_ORDER`（逗号分隔，小写化，保持配置顺序，不去重）。
- 其他任务来自 `<TaskSpec.provider_config_prefix>_PROVIDER` 与 `_FALLBACK_PROVIDER`，忽略大小写去重。
- 不在 `TASK_SPECS` 里的任务抛 `Unsupported AI task`。

`get_models_for_task(provider, task)` 返回主模型和备用模型（去空白、去重）。
`run_ai_task` 对每个 provider 按顺序尝试：缺少任务要求的能力、未知 provider、没有配置模型的 provider
会被记录并跳过；上下文超限不换 provider；其余失败换下一个模型或 provider，全部失败抛 `RuntimeError`。
翻译、vision、classifier 与 advisor 通过 `run_ai_task` 调用；摘要与空闲回顾直接使用同一套顺序与模型解析，
再各自运行带专用工具的工具循环。

## 主聊天的 fallback

`router._try_ai_services` 对 `get_provider_order_for_task("chat")` 的每个 provider：

1. 跳过处于熔断冷却中的 provider。熔断由 `AI_PROVIDER_CIRCUIT_FAILURE_THRESHOLD`、
   `AI_PROVIDER_CIRCUIT_WINDOW_SECONDS`、`AI_PROVIDER_CIRCUIT_COOLDOWN_SECONDS` 控制；
   只有普通的 provider 失败计入，上下文超限、部分失败、已发送内容后的失败与安全拦截不计入。
2. 消息含图片而该 provider 不支持视觉（`chat_service_supports_vision`）时，改用纯文本历史
   （`text_fallback_messages`，没有则去掉图片）。
3. 直接 `await` `chat_provider.run_chat_provider`（原生 async，没有线程池），并设置工具请求上下文，结束后清理沙箱与上下文。
   传入的整轮 `Deadline` 一路带到工具循环；到期时返回固定的超时提示（`TURN_DEADLINE_ERROR_MESSAGE`，进程停止时是
   `TURN_SHUTDOWN_ERROR_MESSAGE`）与已有的工具日志，不再换下一个 provider，也不重试已经开始的工具。规则见 [runtime.md](runtime.md)。
4. 失败分类：安全拦截按上一节处理；上下文超限返回固定的 `CONTEXT_BUDGET_ERROR_MESSAGE`；
   工具已经执行过的部分失败返回 `PARTIAL_AI_RESPONSE_ERROR_MESSAGE` 而不重试（避免重复工具副作用）；
   已发送可见内容后的失败不重试；其他失败记录后换下一个 provider。

所有 provider 失败后，如果消息含图片，用纯文本历史把整个顺序再走一遍；仍然失败返回
`AI_SERVICE_ERROR_MESSAGE`。截止时间到期时不再走这一遍。`router.runtime_error_cause` 把这三条固定文案（以及上面两条超时提示）识别成错误通知，
对话入口据此不把它们写成 assistant 记录。

## 增加或修改 provider

1. 在 `PROVIDERS` 增加 `ProviderSpec`，在 `AppSettings` 增加它引用的全部配置字段（含各任务的
   `*_MODEL`），并更新 `.env.example`。
2. 协议上的特殊要求通过 `ProviderSpec` 的字段表达；确实需要新的开关时，在声明表加字段、在唯一的
   消费点读取，不要在路由或业务模块里按名字分支。
3. `tests/test_ai_provider_registry.py` 会检查配置名都存在、名字与别名不冲突。

## 历史

早期设计曾提出自建 adapter 类层次；实际实现选择了 LiteLLM SDK 加这张声明表，设计文本在 git 历史里。
