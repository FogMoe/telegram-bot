# 敏感数据处理

本页是敏感数据脱敏策略的契约：策略定义在哪里、哪些输出和存储路径必须经过它、运维要注意什么。
具体的匹配规则以代码为准，不在这里复述。

## 单一来源

策略只定义在 `modules/core/redaction.py`。新增敏感命令、凭据格式或脱敏规则只改这个文件，
各路径通过下表的入口调用它，不各自实现。

| 能力 | 符号 |
|---|---|
| 敏感命令集合 | `SENSITIVE_ARGUMENT_COMMANDS`（参数是凭据）、`SENSITIVE_OUTPUT_COMMANDS`（回复是凭据）、`PRIVATE_ONLY_COMMANDS` |
| 命令文本脱敏（支持 `/cmd@BotName`、大小写不敏感） | `redact_command_text`、`has_sensitive_arguments`、`command_secret_values` |
| 自由文本脱敏 | `redact_text`、`redact_output`、`message_sanitizer` |
| 运行时已知密钥 | 从 `core.config` 中名称以 `_API_KEY`、`_API_TOKEN`、`_BOT_TOKEN`、`_SECRET`、`_PASSWORD` 等结尾的字符串配置读取；`register_secret` 登记额外的值 |
| 安全的异常描述 | `describe_exception`（给用户或工具）、`log_exception`（诊断日志）、`report_error`（日志加用户回复里的参考行） |
| 日志脱敏 | `RedactingFilter` |
| 卡密掩码 | `mask_secret` |

占位符固定为 `[redacted]`（`REDACTED`），数据库迁移改写历史数据时使用同一个值。

## 覆盖路径

| 路径 | 入口 | 处理 |
|---|---|---|
| 个人历史：用户命令 | `core/telegram_history.py` 的 `record_command_update` | 凭据类命令只保留命令名，并标记 `redacted="true"` |
| 个人历史：bot 回复与回调提示 | `_record_bot_message`、`_record_callback_answer` | `redact_output`：替换该命令的参数原值和自由文本凭据；`create_code` 的回复整体替换为占位文本 |
| 群聊历史：用户命令与 bot 回复 | `core/group_chat_history.py` 的 `log_group_message` | 所有调用方默认经过 `message_sanitizer`；bot 回复路径带上命令上下文再清洗 |
| 对话历史：工具结果与工具调用参数 | `features/ai/tool_history.py` 的 `tool_logs_to_record_entries` | 写入前 `redact_text`；工具调用参数里的 `/charge` 等命令文本同样处理 |
| 对话历史：AI 最终回复 | `features/conversation/handlers.py` | 写入前 `redact_text` |
| 诊断日志 | `core/bot_logging.py` 的 `configure_logging` | `RedactingFilter` 挂在根 logger 的所有 handler 上，处理格式化后的消息和异常 traceback，对第三方库的日志同样生效 |
| 代码主动记录的异常 | 各模块 | 用 `log_exception` 或 `report_error`，细节在写入日志前已脱敏，不依赖 handler 上是否有 filter |
| 用户与工具可见的错误 | `app/error_handler.py`、各 handler 的 `except` 分支、`features/ai/tools/` | 只返回异常类型加脱敏、截断的概要，或通用文案加错误参考 ID，不回显原始异常文本 |
| 落库的失败原因 | `features/ai/scheduler.py`、`features/ai/idle_followup.py` | `describe_exception(limit=500)` |
| 全局错误处理 | `app/error_handler.py` | 日志只记 `update_id`、更新类型、chat 与 user 标识，不记完整 `Update` |

不脱敏的内容：用户发给 AI 的普通消息保持原文写入个人历史，模型需要读到它们；
只有凭据类命令例外。群聊历史是纯存储路径，所有文本都经过脱敏。

## 错误参考 ID

`new_error_ref()` 生成形如 `ERR-1A2B3C4D` 的 ID。同一个 ID 同时出现在用户可见的回复和对应的诊断日志里，
管理员据此在日志中定位经过脱敏的详情。工具结果里则带异常类型、脱敏概要和 `(ref: ...)`，
让模型能区分参数错误、限流、网络失败等类别。

## 凭据类命令只允许私聊

`/charge`、`/webpassword`、`/create_code` 由 `core/command_privacy.py` 的 `private_chat_only` 装饰，
在群聊等非私聊环境里：

- 不执行命令，也不读取参数。
- 回复提示改用私聊。
- 尽力删除用户发出的那条消息；机器人没有删除权限时只记一条脱敏日志。
- 由 AI 工具代执行的命令不删除消息，因为 `message_id` 指向用户触发 AI 的原消息。
- 这条消息仍会按上面的策略写入群聊历史，其中不含参数原值。

## 密码存储

Web 密码用 Argon2id（`argon2-cffi` 的 `PasswordHasher` 默认参数）哈希，存 PHC 字符串；
盐和参数都编码在字符串里，列宽 `VARCHAR(255)` 足够。`web_password.py` 提供
`hash_password`、`verify_password` 和 `password_needs_rehash`。无盐 SHA-256 的旧哈希不再被识别，
`verify_password` 对它们一律返回 `False`。

## 运维注意事项

- 脱敏只作用于新写入的内容。升级前产生的日志文件（`logs/` 下的当前文件与轮转备份）
  可能含有旧卡密、带 `api_key` 的 URL，以及 httpx 在 INFO 级别记录的含 bot token 的请求 URL。
  需要清理或归档这些文件，并轮换已经可能暴露的凭据：`TELEGRAM_BOT_TOKEN`、各 provider 与 SerpApi 的 key、
  日志里出现过的未使用卡密。
- 已存入数据库的旧历史（群聊历史里的 `/charge`、`/webpassword` 参数，旧的 SHA-256 密码行）
  由数据保留迁移处理，见 `alembic/versions/` 中的 privacy retention 迁移。
- 脱敏基于模式和已知值，无法覆盖任意形式的密钥。新增密钥配置时沿用 `*_API_KEY`、`*_TOKEN`、`*_SECRET`、
  `*_PASSWORD` 的命名，或调用 `register_secret`；少于 `MIN_KNOWN_SECRET_LENGTH` 的值不做精确匹配。
- 日志里的 `ref=ERR-...` 是排查入口；日志仍保留异常类型、位置和脱敏后的消息，`LOG_LEVEL` 不影响脱敏。

## 变更检查

新增一个会接触凭据的命令、工具或日志点时：

1. 命令参数或回复是凭据：把命令名加入 `SENSITIVE_ARGUMENT_COMMANDS` 或 `SENSITIVE_OUTPUT_COMMANDS`，
   并给 handler 加 `@private_chat_only("<command>")`；`PRIVATE_ONLY_COMMANDS` 随之更新。
2. 用户或工具可见的错误：用 `report_error` 或 `describe_exception`，不要拼接 `str(exc)`。
3. 在 `tests/test_sensitive_history.py` 的模式下补一条个人历史与群聊历史同时不含原值的用例。
