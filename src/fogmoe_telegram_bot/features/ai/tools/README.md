# AI 工具

该目录包含 AI 聊天层使用的 OpenAI 兼容工具定义与处理函数。

## 目录结构

- `specs.py`：所有 AI 工具的唯一声明表（`TOOL_SPECS`、`ToolSpec`、`MediaToolSpec`）与可见性解析（`resolve_agent_tools`）
- `models.py`：工具参数模型与 JSON Schema 生成（`AI_TOOL_ARG_MODELS` 由 `specs.py` 登记）
- `schemas.py` / `registry.py`：兼容导出层，从 `specs.py` 派生 `OPENAI_TOOLS`、`AI_TOOL_HANDLERS` 与子 agent 工具集
- `rate_limit.py`：工具共用的滑动窗口限流、非阻塞全局并发门闸与用户会话冷却门闸
- `context.py`：每次请求的上下文存储（用户/群组/消息元信息）
- `telegram_command_tools.py`：代用户执行显式白名单内的 Telegram 命令（策略表在 `../telegram_command_policy.py`）
- `advisor_tools.py`：只读高级推理顾问工具（无工具权限，不自动携带聊天历史）
- `http_tools.py`：外部 HTTP 工具（SerpApi、Jina Reader + Trafilatura 正文抽取）
- `image_tools.py`：图片生成工具（可配置接口，保存生成图片供发送层使用）
- `voice_tools.py`：语音生成工具（保存生成音频供发送层使用）
- `code_tools.py`：Judge0 执行工具
- `sandbox_tools.py`：临时 Linux 沙箱工具
- `user_tools.py`：用户/金币/好感/印象相关工具
- `memory_tools.py`：群聊上下文、永久摘要与用户日记工具
- `summary_tools.py`：摘要助手使用的历史摘要 BM25 检索工具
- `schedule_tools.py`：定时私聊消息的创建/查询/取消
- `sticker_tools.py`：贴纸包与可用 emoji 查询
- `doc_tools.py`：内部参考文档查阅（`src/fogmoe_telegram_bot/resources/docs/*.md`）
- `filename_utils.py`：文件名清理辅助函数（非工具）

## 添加新工具

提示词、工具描述和参数说明的分层规范见
[`docs/ai-tool-prompt-guidelines.md`](../../../../docs/ai-tool-prompt-guidelines.md)。

1) 在合适模块里实现工具函数（必要时新建模块；配置在调用时从 `core.config` 读取，限流复用 `rate_limit.py`）。
2) 在 `models.py` 定义 Pydantic 参数模型。
3) 在 `specs.py` 的 `TOOL_SPECS` 中登记一条 `ToolSpec`（即时向 Telegram 发送媒体的工具用 `MediaToolSpec`），声明 `arg_model`、`handler`、开放的 `agents` / `entries` / `chat_types`、配置检查 `is_configured`、副作用类型 `side_effect_kind` 与外部计费分类 `billing_category`。`AI_TOOL_ARG_MODELS`、`OPENAI_TOOLS`、`AI_TOOL_HANDLERS`、各子 agent 工具集与文档工具目录都会自动从 `TOOL_SPECS` 推导。
4) 只有在其他模块需要直接调用时，才在 `__init__.py` 里导出。
5) 在 [`docs/ai-conversation-flow.html`](../../../../docs/ai-conversation-flow.html) 的「工具箱」里给新工具补一句说明，再运行 `uv run python scripts/gen_ai_flow_doc.py` 更新工具目录与数字（`tests/test_ai_flow_doc.py` 会检查两者）。

## 注意事项

- 工具处理函数可以是 async 函数、同步函数（走有界线程适配器）或用 `@inline_tool` 标记的内联函数，区分规则见 `dispatch.py`，由工具循环的 `../tool_loop_execution.py` 调用；都返回可 JSON 序列化的 dict。
- 依赖聊天/用户上下文的工具务必通过 `context.get_tool_request_context()` 读取。
- 尽量避免长耗时网络调用，超时设置要保守。

## fetch_url

`fetch_url` 通过 Jina Reader 拉取页面，再用 Trafilatura 抽取正文。返回字段包括 `content` 和 `truncated`；超过 `FETCH_URL_MAX_CHARS`（默认 12000）时截断。多页探索和长文综合不走这个工具，后续会单独增加研究工具，由 subagent 完成检索并只返回结论。

## advisor 配置

`advisor` 默认不绑定模型，必须显式配置 advisor provider 和对应的任务模型。例如：

```env
AI_ADVISOR_PROVIDER=openai
AI_ADVISOR_FALLBACK_PROVIDER=gemini
OPENAI_ADVISOR_MODEL=your-senior-model
GEMINI_ADVISOR_MODEL=your-fallback-model
```

各 provider 对应的模型变量为：

- `OPENAI_ADVISOR_MODEL`
- `OPENROUTER_ADVISOR_MODEL`
- `FOGMOE_ADVISOR_MODEL`
- `GEMINI_ADVISOR_MODEL`
- `ZHIPU_ADVISOR_MODEL`
- `AZURE_OPENAI_ADVISOR_MODEL`
- `SILICONFLOW_ADVISOR_MODEL`

资源控制可通过以下配置调整：

- `AI_ADVISOR_TIMEOUT_SECONDS`：单次 advisor 请求超时，默认 120 秒。
- `AI_ADVISOR_MAX_CALLS_PER_REQUEST`：每次用户请求最多调用次数，默认 1。
- `AI_ADVISOR_RATE_LIMIT_WINDOW_SECONDS`：用户限流窗口，默认 300 秒。
- `AI_ADVISOR_RATE_LIMIT_MAX_CALLS`：每名用户在窗口内的最大调用数，默认 3。
- `AI_ADVISOR_MAX_CONCURRENT_REQUESTS`：进程内 advisor 最大并发数，默认 3。

生图、语音与 Linux 沙箱同样通过 `rate_limit.py` 读取运行时配置（`AI_IMAGE_RATE_LIMIT_*`、`AI_VOICE_RATE_LIMIT_*`、`AI_SANDBOX_RATE_LIMIT_*`、`AI_SANDBOX_COOLDOWN_SECONDS`、`AI_SANDBOX_MAX_CONCURRENT_SESSIONS`），详见仓库根目录 `.env.example`。
