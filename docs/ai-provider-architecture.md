# AI Provider 契约

本页是 AI provider 的契约：provider 在哪里声明、任务如何解析到 provider 与模型、主聊天如何 fallback、
provider 特有的协议要求由什么承载。权威定义是代码，本页只讲规则并指向符号，不复述字段值。
环境变量以 [`.env.example`](../.env.example) 为准。

## 单一声明表

所有 provider 只在 `src/fogmoe_telegram_bot/core/ai_providers.py` 的 `PROVIDERS` 里声明一次，任务在同一文件的
`TASK_SPECS` 里声明。其他代码都从这里读取，不再各自维护 provider 表：

| 用途 | 入口 | 从声明表读取 |
|---|---|---|
| provider 名字与别名解析 | `ai_providers.lookup` / `require`、`core/litellm_models.py` 的 `normalize_provider` | `name`、`aliases` |
| LiteLLM 模型名 | `ai_providers.litellm_model_name`（`core/litellm_models.py` 转发） | `litellm_prefix`、`prefix_rule`、`openai_compatible_flag` |
| 认证与端点参数 | `features/ai/litellm_provider_config.py` 的 `provider_params` | `credentials` |
| 任务的 provider 顺序与模型 | `features/ai/provider_resolver.py` | `TASK_SPECS`、`model_prefix`、`fallback_models` |
| 所有任务的执行与回退 | `features/ai/task_runner.py` 的 `run_with_fallback`（主聊天、摘要、空闲回顾）与 `run_ai_task`（一次模型调用） | `TaskSpec.requires`、`capabilities`、`translates_safety_blocks`、`safety_blocks_fall_through` |
| 主聊天的每次尝试、图片降级与失败提示 | `features/ai/router.py` | 模型、`display_name`、`skip_tools`、视觉能力 |
| 历史消息的协议整理 | `features/ai/litellm_message_sanitizer.py` | `wire_protocol` |
| 工具循环里的协议兼容 | `features/ai/tool_loop_messages.py` 的 `resolve_assistant_message` | `merges_split_tool_call_choices` |
| 聊天模型是否支持图片 | `features/ai/chat_capabilities.py` | `capabilities`、`AI_CHAT_TEXT_ONLY_MODELS` |
| token 计数使用的模型 | `core/chat_records.py` 的 `_chat_token_count_model` | `configured_models` |

声明里引用的每个配置名都必须是 `core/config.py` 的 `AppSettings` 字段，
`tests/test_ai_provider_registry.py` 逐项检查。

声明表、任务解析与认证参数都有可选的 `settings` 参数，默认读 `core.config` 的当前值；
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
- **备用模型（`fallback_models`）**：`run_with_fallback` 在主模型失败后尝试备用模型，不换模型的失败见「回退规则」。
- **安全拦截**：`translates_safety_blocks` 让 `run_with_fallback` 把「被安全策略拦截」的错误转成 `SafetyBlockError`：
  还有备用模型就换模型；模型用完时，`safety_blocks_fall_through` 让它换下一个 provider（不计熔断），
  没有声明的 provider 直接抛出。执行过工具或发出过可见内容之后的拦截按「工具之后的失败」处理，不再重试。
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

## 回退规则

所有任务共用 `task_runner.run_with_fallback`，每次尝试做什么由调用方给出：翻译、vision、classifier 与 advisor
经 `run_ai_task` 做一次模型调用；主聊天、摘要与空闲回顾各自跑一次带自己工具的工具循环。对顺序里的每个 provider：

1. 未知 provider、缺少任务要求的能力（`TaskSpec.requires`：chat、summary、recap 要工具，vision 要视觉）、
   没有配置模型、处于熔断冷却中的，记录后跳过。
2. 依次尝试主模型和备用模型，第一次成功就返回。
3. 不换模型、原样上抛的失败：上下文超限（与 provider 无关，原因链里有 `ContextBudgetExceededError` 也算）、
   整轮截止时间到期、执行过工具或发出过可见内容之后的失败（`PartialAIResponseError`）。
   摘要与空闲回顾的工具都是只读的（`tools_are_read_only`），工具之后的失败仍然换下一个模型从头重跑。
4. 安全拦截按上一节处理；其余失败换下一个模型，模型用完换下一个 provider。
5. 传了熔断的任务（目前只有主聊天）在一个 provider 的模型都以普通失败结束时记一次熔断失败，成功时清零；
   上下文超限、工具之后的失败与安全拦截不计入。
6. 传了整轮截止时间的任务（目前只有主聊天）在看每个 provider（包括要跳过的）、试每个模型之前检查它，
   到期就抛 `TurnDeadlineError`（`phase=fallback`），不再尝试，也不记熔断失败。

全部失败或被跳过时抛 `AllProvidersFailedError`，`last_error` 是最后一次失败。

## 主聊天的 fallback

`router.get_ai_response` 把一轮对话交给 `run_with_fallback("chat", ...)`，熔断用 `CHAT_CIRCUIT`，阈值是
`AI_PROVIDER_CIRCUIT_FAILURE_THRESHOLD`、`AI_PROVIDER_CIRCUIT_WINDOW_SECONDS`、`AI_PROVIDER_CIRCUIT_COOLDOWN_SECONDS`。
同时传入整轮 `Deadline`：回退链走到一半时间已经用完（包括后面的 provider 全都会被跳过的情况），返回固定的超时提示，
不给没被调用过的 provider 记失败（`phase=fallback`）。每次尝试（`_run_chat_candidate`）：

1. 消息含图片而该 provider 不支持视觉（`chat_service_supports_vision`）时，改用纯文本历史
   （`text_fallback_messages`，没有则去掉图片）。
2. 在一份新的工具请求上下文里（`tool_request_scope`）直接 `await` 工具循环（原生 async，没有线程池），
   带上声明里的展示名与 `skip_tools`，结束后清理沙箱（清理失败只记日志，不影响这次的结果）。
   传入的整轮 `Deadline` 一路带到工具循环，等待模型时到期给这个 provider 记一次熔断失败。规则见 [runtime.md](runtime.md)。

执行结果转成固定的提示文案：截止时间到期返回 `TURN_DEADLINE_ERROR_MESSAGE`（进程停止时是 `TURN_SHUTDOWN_ERROR_MESSAGE`）
与已有的工具日志；上下文超限返回 `CONTEXT_BUDGET_ERROR_MESSAGE`；执行过工具之后的失败返回
`PARTIAL_AI_RESPONSE_ERROR_MESSAGE` 与工具日志，用户已经看到可见内容或生成的媒体时（`delivered_to_user`）
不再附加提示。「是否已有副作用」只由工具循环的日志判断（`has_side_effects`）：工具循环把这之后的任何异常
都包成 `PartialAIResponseError`，所以不会换模型或 provider 把已经执行过的工具再跑一遍。

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
