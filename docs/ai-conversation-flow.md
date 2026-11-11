# AI 对话流程：代码对照

流程图在 [ai-conversation-flow.html](ai-conversation-flow.html)，用浏览器打开。那一页只用自然语言讲流程，
本页按同样的章节列出每一步对应的代码，并给出完整的工具目录与阈值表。
各环节的规则分别在 [architecture.md](architecture.md) 的「一轮对话」、
[ai-provider-architecture.md](ai-provider-architecture.md)（provider、fallback）与 [runtime.md](runtime.md)（准入、截止时间）里。

权威定义是代码。两份文档里会随代码变化的部分由 `scripts/gen_ai_flow_doc.py` 同步：本页标着「自动生成」的块，
以及 HTML 页面里的数字。改了工具、阶段或阈值之后运行：

```bash
uv run python scripts/gen_ai_flow_doc.py
```

`tests/test_ai_flow_doc.py` 检查：

- 两份文档里同步的部分没有过期；
- HTML 页面的九个步骤与 `Stage` 一一对应，工具箱与各 agent 实际开放的工具一一对应（加了工具就要在页面上补一句说明）；
- 本页用反引号引用的符号、文件，以及两份文档里的相对链接都还存在。

## 1. 进门

| 步骤 | 代码 | 文件 |
|---|---|---|
| 历史入口：设置出站事件来源，记录命令与按钮事件；bot 发出的消息也记入历史 | `prepare_update_history`、`HistoryTrackingExtBot` | [core/telegram_history.py](../src/fogmoe_telegram_bot/core/telegram_history.py) |
| 对话入口、排队、建立请求 | `reply`、`_reply_locked`、`_reply_batch_unlocked`、`_build_turn_request` | [conversation/handlers.py](../src/fogmoe_telegram_bot/features/conversation/handlers.py) |
| 编辑过但内容没变的消息 | `_record_message_content_and_check_unchanged_edit` | [conversation/messages.py](../src/fogmoe_telegram_bot/features/conversation/messages.py) |
| 攒成一批 | `_message_batch_key`、`_MESSAGE_BATCHES` | [conversation/batching.py](../src/fogmoe_telegram_bot/features/conversation/batching.py) |
| 每用户待处理数、全局名额、拒绝时的提示 | `AdmissionController`、`user_pending`、`slot`、`_reject_unstarted_batch` | [core/admission.py](../src/fogmoe_telegram_bot/core/admission.py)、[conversation/handlers.py](../src/fogmoe_telegram_bot/features/conversation/handlers.py) |
| 同一个人同一时间只有一轮 | `get_conversation_lock` | [ai/conversation_locks.py](../src/fogmoe_telegram_bot/features/ai/conversation_locks.py) |
| 群聊记录、是否点名 bot | `log_group_message`（表 `chat_records_group`）、`_group_batch_wants_a_reply` | [core/group_chat_history.py](../src/fogmoe_telegram_bot/core/group_chat_history.py)、[conversation/handlers.py](../src/fogmoe_telegram_bot/features/conversation/handlers.py) |
| 聊天冷却 | `check_chat_cooldown` | [core/command_cooldown.py](../src/fogmoe_telegram_bot/core/command_cooldown.py) |
| 整轮限时 | `Deadline`，在 `_reply_locked` 里开始计时 | [core/deadline.py](../src/fogmoe_telegram_bot/core/deadline.py) |

## 2. 一轮对话

阶段顺序（自动生成，来自 `Stage`）：

<!-- BEGIN GENERATED: stages -->
`plan` → `charge` → `context` → `prepare` → `history_in` → `model` → `history_out` → `delivery` → `finalize`
<!-- END GENERATED: stages -->

| 步骤 | 代码 | 文件 |
|---|---|---|
| 总流程与计时 | `ConversationTurn.run`、`StageTimer`、`run_turn` | [conversation/turn.py](../src/fogmoe_telegram_bot/features/conversation/turn.py)、[conversation/turn_types.py](../src/fogmoe_telegram_bot/features/conversation/turn_types.py) |
| 估价、扣费 | `_plan`、`_charge`、`charge_turn` | [conversation/billing.py](../src/fogmoe_telegram_bot/features/conversation/billing.py) |
| 了解用户 | `_load_context`、`user_state_prompt`、`format_user_state_prompt` | [ai/user_state.py](../src/fogmoe_telegram_bot/features/ai/user_state.py)、[core/prompt_utils.py](../src/fogmoe_telegram_bot/core/prompt_utils.py) |
| 看图（历史里只存描述，原图通过 `runtime_replacements` 只给这一次请求） | `_prepare_inputs` | [conversation/turn.py](../src/fogmoe_telegram_bot/features/conversation/turn.py) |
| 记下用户的话 | `_record_input` → `insert_records`、`get_history` | [conversation/turn_services.py](../src/fogmoe_telegram_bot/features/conversation/turn_services.py) |
| 思考与动手 | `_run_model` → `prepare_model_turn` → `run_model`（`ModelRequest` / `ModelResponse`）；工具上下文 `build_tool_context`，回复整理 `finish_reply` | 见第 3 节；[ai/turn_pipeline.py](../src/fogmoe_telegram_bot/features/ai/turn_pipeline.py)、[ai/tools/context.py](../src/fogmoe_telegram_bot/features/ai/tools/context.py)、[ai/outbound.py](../src/fogmoe_telegram_bot/features/ai/outbound.py) |
| 记下做了什么 | `execute_post_input_turn` → `_record_output`、`turn_messages_to_record_entries`、`reply_record_entries` | [ai/turn_pipeline.py](../src/fogmoe_telegram_bot/features/ai/turn_pipeline.py)、[ai/tool_history.py](../src/fogmoe_telegram_bot/features/ai/tool_history.py) |
| 发出回复 | `_deliver`、`_deliver_messages` | [conversation/turn.py](../src/fogmoe_telegram_bot/features/conversation/turn.py) |
| 收尾 | `_finalize`、`USER_ENTRY_POLICY`、`archive_completed_clear`、`flush_events` | [ai/turn_pipeline.py](../src/fogmoe_telegram_bot/features/ai/turn_pipeline.py)、[conversation/turn_services.py](../src/fogmoe_telegram_bot/features/conversation/turn_services.py) |
| 每次写入历史后：打包发送被淘汰的归档、容量提示、溢出时生成摘要 | `_persist_records` | [conversation/turn.py](../src/fogmoe_telegram_bot/features/conversation/turn.py) |

## 3. 思考与动手

| 步骤 | 代码 | 文件 |
|---|---|---|
| 每次尝试跑一次工具循环（带上声明里隐藏的工具）、看不了图时换成文字、失败转成提示 | `get_ai_response`、`_try_ai_services`、`_run_chat_candidate`、`_messages_for_service`、`skip_tools` | [ai/router.py](../src/fogmoe_telegram_bot/features/ai/router.py) |
| 按顺序换 provider 与备用模型、安全拦截、熔断（主聊天、摘要、回顾与 `run_ai_task` 共用） | `run_with_fallback`、`ProviderCircuit`、`CHAT_CIRCUIT` | [ai/task_runner.py](../src/fogmoe_telegram_bot/features/ai/task_runner.py) |
| 拼好上下文（人设 + 用户状态） | `compose_system_prompt` | [ai/prompts.py](../src/fogmoe_telegram_bot/features/ai/prompts.py) |
| 工具循环、边做边说、工具执行后的重试 | `run_tool_loop`、`emit_visible_content`、`_create_chat_completion_with_post_tool_retries`、`resolve_assistant_message` | [ai/tool_runner.py](../src/fogmoe_telegram_bot/features/ai/tool_runner.py)、[ai/tool_loop_delivery.py](../src/fogmoe_telegram_bot/features/ai/tool_loop_delivery.py)、[ai/tool_loop_messages.py](../src/fogmoe_telegram_bot/features/ai/tool_loop_messages.py) |
| 一次工具调用：参数校验、执行、媒体立刻发出、只把公开部分交回模型、附带的事件 | `_validate_tool_args`、`_call_tool`、`send_media_result_immediately`、`public_tool_result`、`context_messages_from_tool_result` | [ai/tool_loop_execution.py](../src/fogmoe_telegram_bot/features/ai/tool_loop_execution.py)、[ai/tool_loop_delivery.py](../src/fogmoe_telegram_bot/features/ai/tool_loop_delivery.py) |
| 时间用完时：结果未知、未执行 | `close_interrupted_round`、`TurnDeadlineError` | [ai/tool_loop_deadline.py](../src/fogmoe_telegram_bot/features/ai/tool_loop_deadline.py)、[ai/tool_runner.py](../src/fogmoe_telegram_bot/features/ai/tool_runner.py) |
| 单次请求上限、按服务的协议整理消息 | `create_chat_completion`、`enforce_messages_context_budget`、`sanitize_messages_for_provider` | [ai/litellm_client.py](../src/fogmoe_telegram_bot/features/ai/litellm_client.py)、[ai/context_budget.py](../src/fogmoe_telegram_bot/features/ai/context_budget.py)、[ai/litellm_message_sanitizer.py](../src/fogmoe_telegram_bot/features/ai/litellm_message_sanitizer.py) |
| 发到 Telegram | `TelegramVisibleContentHandler` | [ai/telegram_visible_sender.py](../src/fogmoe_telegram_bot/features/ai/telegram_visible_sender.py) |

出问题时的处理规则见 [ai-provider-architecture.md](ai-provider-architecture.md) 的「回退规则」与「主聊天的 fallback」：
上下文超限 `ContextBudgetExceededError`、执行过工具或发出过可见内容之后失败 `PartialAIResponseError`（由 `has_side_effects` 判断）、
安全拦截 `SafetyBlockError`。

## 4. 工具箱

所有工具在 `TOOL_SPECS`（[ai/tools/specs.py](../src/fogmoe_telegram_bot/features/ai/tools/specs.py)）单一声明，每次运行由 `resolve_agent_tools` 按入口、聊天类型、provider 的 `skip_tools` 与配置完备性筛出本次可见的 schema 与处理函数。兼容层导出的主聊天集合是 `OPENAI_TOOLS` 与 `AI_TOOL_HANDLERS`（[ai/tools/schemas.py](../src/fogmoe_telegram_bot/features/ai/tools/schemas.py)、
[ai/tools/registry.py](../src/fogmoe_telegram_bot/features/ai/tools/registry.py)）；空闲回顾用 `IDLE_RECAP_TOOLS`，
摘要助手用 `SUMMARY_TOOLS`。参数模型在 `AI_TOOL_ARG_MODELS`，工具读取请求上下文用 `get_tool_request_context`。
加工具的步骤见 [ai/tools/README.md](../src/fogmoe_telegram_bot/features/ai/tools/README.md)，描述怎么写见
[ai-tool-prompt-guidelines.md](ai-tool-prompt-guidelines.md)。

工具目录（自动生成）：

<!-- BEGIN GENERATED: tools -->
共 20 个工具：主聊天 18 个，空闲回顾 3 个，摘要 1 个。参数后面带 `?` 的是可选参数。

| 工具 | 谁能调用 | 参数 | 处理函数 | 描述（模型看到的原文） |
|---|---|---|---|---|
| `get_help_text` | 主聊天 | — | `memory_tools.get_help_text_tool` | Returns a list of available Telegram commands and features for users |
| `execute_telegram_command` | 主聊天 | `command` | `telegram_command_tools.execute_telegram_command_tool` | Run a Telegram command as the current user in the current chat. The normal command handler sends its mechanical reply directly, and its events enter the conversation before the next model response. Returns execution status only. |
| `read_doc` | 主聊天 | `topic?` | `doc_tools.read_doc_tool` | Read an internal reference document about how this bot's own features work. Omit topic to list every available document with a one-line summary, then call again with a topic to read that one in full. Each fact is documented in exactly one topic and the others defer to it, so follow a cross-reference rather than infer what it omits. Overly long documents are truncated. |
| `list_available_stickers` | 主聊天 | `pack_name?` | `sticker_tools.list_available_stickers_tool` | List configured Telegram sticker packs, their summaries, and currently available emoji choices. Use this before adding sticker directives to a reply. |
| `google_search` | 主聊天 | `query`, `detailed?`, `show_full_json?` | `http_tools.google_search_tool` | Use Google search engine to obtain the latest information and answers |
| `advisor` | 主聊天 | `task`, `case_facts?` | `advisor_tools.advisor_tool` | Submit one complete reasoning task to a read-only senior advisor. Put the question or decision in task. Use case_facts only for facts, evidence, options, and constraints specific to that task. Each call is a single-turn consultation and cannot receive follow-up messages. The advisor cannot use tools or take actions. Its response is advisory material for you to evaluate and synthesize. |
| `fetch_group_context` | 主聊天 | `window_size?` | `memory_tools.fetch_group_context_tool` | Fetch message history from group chat (group chats only) |
| `fetch_url` | 主聊天 | `url` | `http_tools.fetch_url_tool` | Fetch a webpage and return extracted main text for up-to-date browsing. Overly long pages are truncated. |
| `execute_python_code` | 主聊天 | `source_code`, `stdin?` | `code_tools.execute_python_code_tool` | Run Python code remotely and return its output. All results must be printed with print(), otherwise they will not appear in the output. |
| `linux_sandbox` | 主聊天 | `command`, `cwd?`, `timeout_seconds?` | `sandbox_tools.linux_sandbox_tool` | Execute a non-interactive shell command in an isolated temporary Linux sandbox. The sandbox preserves filesystem state across linux_sandbox calls in the same user request, then closes automatically. It lives at most 5 minutes and accepts at most 10 calls per request. Once it closes, the same user cannot open another sandbox for 5 minutes. |
| `generate_image` | 主聊天 | `prompt`, `width?`, `height?`, `steps?`, `seed?`, `timeout_seconds?` | `image_tools.generate_image_tool` | Generate exactly one image from a text prompt. The image is sent to Telegram immediately after the call succeeds. |
| `generate_voice` | 主聊天 | `text` | `voice_tools.generate_voice_tool` | Generate exactly one spoken audio clip from text. The audio is sent to Telegram immediately after the call succeeds. |
| `kindness_gift` | 主聊天 | `amount?` | `user_tools.kindness_gift_tool` | Gift coins to the user. Each user can receive one gift per 24 hours; calling again inside that window gifts nothing and returns a cooldown status. Omit amount to let the system pick one at random. |
| `update_impression` | 主聊天 | `impression` | `user_tools.update_impression_tool` | Replace the stored impression of the user. This overwrites the previous impression entirely instead of appending to it, so carry over everything still worth keeping; the current text is visible as &lt;impression&gt; in the user profile. |
| `fetch_permanent_summaries` | 主聊天、空闲回顾 | `start?`, `end?` | `memory_tools.fetch_permanent_summaries_tool` | Fetch user's historical conversation summaries (newest on top, max 5 results per request) |
| `search_permanent_records` | 主聊天、空闲回顾 | `pattern`, `limit?`, `oldest_first?` | `memory_tools.search_permanent_records_tool` | Search user's permanent chat snapshots with a regex pattern |
| `schedule_ai_message` | 主聊天 | `action?`, `timestamp_utc?`, `recurrence_unit?`, `recurrence_interval?`, `trigger_reason?`, `context?`, `instruction?`, `schedule_id?` | `schedule_tools.schedule_ai_message_tool` | Schedule, list, or cancel one-time or recurring private messages for the user. Use recurrence parameters when creating recurring schedules. UTC timestamps only. Max 3 pending tasks, max 12 total (older tasks are overwritten). |
| `user_diary` | 主聊天 | `action?`, `page?`, `content?`, `title?`, `summary?`, `start_line?`, `end_line?`, `line_numbers?` | `memory_tools.user_diary_tool` | Read or update the internal diary for the current user. For existing pages, start with index to see compact titles and summaries, then read relevant pages. Use patch with start_line/end_line to replace those lines; append adds content at the end. Every content update requires a current summary, and new or previously untitled pages also require a title. When a page exceeds its size limit, the oldest content is truncated. |
| `read_diary_page` | 空闲回顾 | `page` | `memory_tools.read_diary_page_tool` | Read one existing diary page selected from diary_index. This tool is read-only and cannot create, edit, or delete diary pages. |
| `search_prior_context` | 摘要 | `query`, `limit?` | `summary_tools.search_prior_context_tool` | Search up to 20 earlier archived summaries with lightweight BM25 ranking and a small recency boost. Use concrete terms from the current snapshot to resolve older references or continuity. |
<!-- END GENERATED: tools -->

## 5. 模型看到什么

| 存储 | 内容 | 主要入口 |
|---|---|---|
| 表 `chat_records` 的 `messages` | 每个用户一份活跃历史（JSON 数组，OpenAI 消息格式）。私聊与群聊共用，`conversation_id` 就是用户 id | `insert_chat_records`、`get_chat_history` |
| 表 `permanent_chat_records` | 归档快照 `conversation_snapshot` 与它的摘要 `summary` | 压缩、`/clear`；工具 `fetch_permanent_summaries`、`search_permanent_records` |
| 表 `chat_records_group` | 群聊里的消息，不进活跃历史 | 工具 `fetch_group_context` |
| 用户状态 | 余额、套餐、权限、印象、日记是否存在 | `format_user_state_prompt`，每次请求拼进 system prompt，不写入历史 |

活跃历史里的消息：

| role | 内容 | 谁写入 |
|---|---|---|
| `user` | 用户消息：`<metadata>` 带聊天类型、时间、用户、消息 id、回复与转发，后面是 `<message>` | `format_user_message` |
| `user` | bot 自己发出的消息、命令回复、按钮事件（`origin` 是 `command_handler` 或 `bot_automation`） | `HistoryTrackingExtBot`、`flush_pending_events` |
| `user` | 历史状态事件 `origin="history_state"`：`new_session`、`near_limit`、`compressed`（之后回填 `<summary>`） | `_build_history_state_event` |
| `user` | 余额状态事件 `origin="coin_service"`：`suspended`、`resumed` | `_build_coin_service_state_event` |
| `assistant` | 回复，以及伴随工具调用的可见文字（都已脱敏） | `_record_output`、`turn_messages_to_record_entries` |
| `assistant` + `tool` | 带 `tool_calls` 的 assistant 消息与对应的 `tool` 结果，`tool_call_id` 一一配对（结果已脱敏） | `turn_messages_to_record_entries` |

一次请求的 messages（示意，字段与格式以代码为准；system 消息只在请求里，不写入历史）：

```jsonc
[
  // run_tool_loop 插入：SYSTEM_PROMPT 后面接 user_state_prompt
  {"role": "system", "content": "……<user_state coins=\"120\" user_plan=\"free\" permission=\"0\" permission_label=\"Normal\" diary_exists=\"true\" />\n<user_profile>…</user_profile>"},

  // 上一次压缩留下的事件，摘要生成后回填 <summary>
  {"role": "user", "content": "<metadata type=\"system\" timestamp=\"2026-11-01 08:00:00\" origin=\"history_state\" history_state=\"compressed\">\n  <summary>……</summary>\n</metadata>"},

  // 用户消息
  {"role": "user", "content": "<metadata type=\"private\" timestamp=\"2026-11-03 09:12:30\" user=\"@alice\" message_id=\"42\">\n</metadata>\n<message>明天东京天气怎么样</message>"},

  // 模型要求调用工具；content 是已经发给用户的可见文字
  {"role": "assistant", "content": "我查一下", "tool_calls": [
    {"id": "call_1", "type": "function", "function": {"name": "google_search", "arguments": "{\"query\": \"东京 明天 天气\"}"}}
  ]},

  // 工具结果，tool_call_id 与上面配对
  {"role": "tool", "tool_call_id": "call_1", "name": "google_search", "content": "{\"results\": […]}"},

  // 最终回复
  {"role": "assistant", "content": "明天东京多云……"}
]
```

## 6. 记录太长时

| 步骤 | 代码 | 文件 |
|---|---|---|
| 每次写入：锁住这一行、清理配不上对的 tool 消息、估算 token、插入 `near_limit` / `new_session` 事件 | `insert_chat_records`、`_insert_chat_records_once`、`_sanitize_messages_with_tool_pairs`、`estimate_conversation_tokens` | [core/chat_records.py](../src/fogmoe_telegram_bot/core/chat_records.py)、[core/token_estimator.py](../src/fogmoe_telegram_bot/core/token_estimator.py) |
| 只留最近的一段，保留它们需要的工具调用 | `_trim_messages_with_tool_context` | [core/chat_records.py](../src/fogmoe_telegram_bot/core/chat_records.py) |
| 存进归档，淘汰超出保留数的旧快照并打包发给用户 | `prune_permanent_records`、`send_permanent_records_archive` | [core/archive_utils.py](../src/fogmoe_telegram_bot/core/archive_utils.py) |
| 溢出时先即时生成摘要，来不及就转后台 | `handle_history_overflow`、`handle_snapshot_created`、`refresh_summary_after_overflow` | [conversation/history_hooks.py](../src/fogmoe_telegram_bot/features/conversation/history_hooks.py)、[ai/summary.py](../src/fogmoe_telegram_bot/features/ai/summary.py) |
| 摘要助手、把摘要放回开头 | `generate_summary_immediately`、`schedule_summary_generation`、`_run_summary_agent`、`async_update_latest_history_state_summary` | [ai/summary.py](../src/fogmoe_telegram_bot/features/ai/summary.py) |
| 清空对话 | `archive_chat_and_start_new_session` | [conversation/clear.py](../src/fogmoe_telegram_bot/features/conversation/clear.py) |
| 金币用完时暂停记录、恢复时补标记 | `_build_coin_service_state_event`，`_finalize` 里以 `suspend_if_zero` 写入 | [core/chat_records.py](../src/fogmoe_telegram_bot/core/chat_records.py) |

## 7. 别的时候

| 入口 | 工具 | 文件 |
|---|---|---|
| 定时消息 | 主聊天全套（经 `get_ai_response`） | [ai/scheduler.py](../src/fogmoe_telegram_bot/features/ai/scheduler.py) |
| 空闲回顾：`_run_recap_agent` 写回顾（`idle_recap` 事件），主模型再写跟进消息 | 回顾：`IDLE_RECAP_TOOLS`；跟进：主聊天全套 | [ai/idle_followup.py](../src/fogmoe_telegram_bot/features/ai/idle_followup.py) |
| 摘要助手 `_run_summary_agent` | `search_prior_context` | [ai/summary.py](../src/fogmoe_telegram_bot/features/ai/summary.py) |

定时消息和空闲跟进的主模型与一轮对话共用同一套收尾流水线（`execute_post_input_turn` 与 `prepare_model_turn`，策略分别是 `SCHEDULE_ENTRY_POLICY` 和 `IDLE_FOLLOWUP_ENTRY_POLICY`，见 [ai/turn_pipeline.py](../src/fogmoe_telegram_bot/features/ai/turn_pipeline.py)）：用户状态提示词 `build_user_state_prompt`、工具上下文
`build_tool_context`（没有聊天字段，带撤销信号；`resolve_agent_tools` 自动隐藏群聊工具与代执行命令）、回复整理 `finish_reply`；历史经 `write_history` 写入
（[ai/history_writer.py](../src/fogmoe_telegram_bot/features/ai/history_writer.py)），回复经 `send_private_reply` 发出。
claim、租约与崩溃恢复见 [job-recovery.md](job-recovery.md)。

## 阈值与上限（自动生成）

默认值取自代码，部署时可以用环境变量覆盖 `AppSettings` 里的项，见 [`.env.example`](../.env.example)。

<!-- BEGIN GENERATED: limits -->
| 名字 | 默认值 | 定义 | 作用 |
|---|---|---|---|
| `CHAT_BATCH_WINDOW_SECONDS` | `1.0` | [core/config.py](../src/fogmoe_telegram_bot/core/config.py) | 同一个人在同一个聊天里连发的消息，等这么多秒后合成一批，只调用一次模型 |
| `CHAT_TURN_DEADLINE_SECONDS` | `360.0` | [core/config.py](../src/fogmoe_telegram_bot/core/config.py) | 整轮截止时间，从进入队列开始计时，覆盖排队、媒体、模型与投递 |
| `CHAT_TOKEN_WARN_LIMIT` | `114000` | [core/config.py](../src/fogmoe_telegram_bot/core/config.py) | 写入用户消息后活跃历史超过它：插入 near_limit 事件并提醒用户 |
| `CHAT_TOKEN_LIMIT` | `120000` | [core/config.py](../src/fogmoe_telegram_bot/core/config.py) | 写入后活跃历史超过它：压缩（裁剪、归档、生成摘要） |
| `CHAT_CONTEXT_HARD_LIMIT_RATIO` | `1.25` | [core/config.py](../src/fogmoe_telegram_bot/core/config.py) | 单次模型请求的硬上限倍数，见下一行 |
| `请求硬上限（推导）` | `150000` | [features/ai/litellm_client.py](../src/fogmoe_telegram_bot/features/ai/litellm_client.py) | CHAT_TOKEN_LIMIT × 倍数；消息 + 工具 schema + 最大输出 + 预留超过它就报错，不删也不截断任何内容 |
| `CHAT_CONTEXT_SAFETY_TOKENS` | `2048` | [core/config.py](../src/fogmoe_telegram_bot/core/config.py) | 估算请求大小时额外预留的 token |
| `run_tool_loop(max_iterations)` | `10` | [features/ai/tool_runner.py](../src/fogmoe_telegram_bot/features/ai/tool_runner.py) | 工具循环最多几轮；用完后不带工具再要一次最终回复 |
| `POST_TOOL_COMPLETION_RETRY_DELAYS_SECONDS` | `(1.0, 3.0)` | [features/ai/tool_runner.py](../src/fogmoe_telegram_bot/features/ai/tool_runner.py) | 执行过工具或发出过可见内容之后，模型调用遇到临时错误时的重试间隔（同一模型，不换 provider） |
| `_trim_messages_with_tool_context(keep_non_tool)` | `10` | [core/chat_records.py](../src/fogmoe_telegram_bot/core/chat_records.py) | 压缩时保留最近几条非 tool 消息；它们引用的 tool_call 会一并保留，保持配对 |
| `PERMANENT_RECORDS_KEEP` | `100` | [core/chat_records.py](../src/fogmoe_telegram_bot/core/chat_records.py) | 每个用户保留的归档快照数（user.permanent_records_limit 可单独设置），更早的打包发给用户后删除 |
| `SUMMARY_IMMEDIATE_TIMEOUT_SECONDS` | `120.0` | [features/ai/summary.py](../src/fogmoe_telegram_bot/features/ai/summary.py) | 压缩时这一轮最多等多久的即时摘要，超时转后台生成 |
| `SUMMARY_CONCURRENCY` | `2` | [features/ai/summary.py](../src/fogmoe_telegram_bot/features/ai/summary.py) | 同时生成的摘要数（即时与后台共用） |
| `SUMMARY_TOOL_MAX_ITERATIONS` | `4` | [features/ai/summary.py](../src/fogmoe_telegram_bot/features/ai/summary.py) | 摘要 agent 的工具循环轮数 |
| `SUMMARY_CONTEXT_HARD_LIMIT_RATIO` | `1.5` | [features/ai/summary.py](../src/fogmoe_telegram_bot/features/ai/summary.py) | 摘要请求的硬上限倍数（快照可能比活跃历史长） |
| `SUMMARY_MAX_TOKENS` | `2500` | [features/ai/summary.py](../src/fogmoe_telegram_bot/features/ai/summary.py) | 摘要正文超过它会被截短 |
| `SUMMARY_RETRY_LIMIT` | `3` | [features/ai/summary.py](../src/fogmoe_telegram_bot/features/ai/summary.py) | 摘要整体重试次数（每次都把 provider 顺序走一遍） |
<!-- END GENERATED: limits -->
