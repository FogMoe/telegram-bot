# 架构约定

## 依赖方向

```
app → features → core
app → core
```

- `modules/main.py` 只负责进程入口。
- `modules/app/` 负责组装 Telegram Application、注册 handler 和 job，并在启动时把业务回调注入 `core`。
- `modules/core/` 只放跨功能共享能力，禁止 import `features` 和 `app`。
- `modules/features/` 放业务功能。功能之间默认不互相 import；必须共享的能力抽到 `core`，或由 `app` 在启动时注入回调。

每个功能模块对外提供 `setup_*_handlers(application)`。组装层只决定注册顺序，不实现业务。

用户可见行为（命令名、过滤器、handler group、job 间隔、扣费和触发规则）不属于架构整理范围，改动前需要单独确认。

## 各层落点

### app

| 文件 | 职责 |
|---|---|
| `bot_app.py` | 构建 Application，挂 `post_init` / `post_stop`；`create_application` 接受显式的 `settings` 与预构建的 `bot`，见「配置注入」 |
| `handler_registry.py` | 注册顺序的唯一来源（`REGISTRATION_STEPS`） |
| `handler_groups.py` | 按功能分组调用各 feature 的 `setup_*`，不实现业务 |
| `error_handler.py` | 全局错误回复，属于运行时而非某个功能；日志与回复的脱敏规则见 [sensitive-data.md](sensitive-data.md) |
| `smoke_check.py` | `main.py --check` 的启动冒烟检查：组装 Application 并注册 handler，不连接 Telegram 和数据库 |

`register_core_command_handlers` 仍在组装层直接 `add_handler`：那一组命令的注册顺序在历史上跨功能交错，
而 `tests/test_handler_registry.py` 把最终顺序当作契约。要改成自注册必须先改这个契约。

### core（无业务）

| 文件 | 职责 |
|---|---|
| `config.py` / `db.py` / `bot_logging.py` | 配置、引擎、日志；配置的覆盖机制见「配置注入」 |
| `ai_providers.py` / `litellm_models.py` | AI provider 的唯一声明表，以及由它决定的名字与 LiteLLM 模型名转换，契约见 [ai-provider-architecture.md](ai-provider-architecture.md) |
| `sql.py` | 通用 SQL 助手：`fetch_one` / `fetch_all` / `execute` 与连接别名 |
| `chat_records.py` | AI 对话历史存储：写入、归档、裁剪、token 预算、history-state 事件 |
| `user_records.py` | user 表的基础查询 |
| `migration_support.py` | Alembic 迁移的支撑代码：数据库 URL 优先级、版本表宽度、可重入 DDL 助手，见 [database-migrations.md](database-migrations.md) |
| `mysql_connection.py` | **兼容层**：把上面三者 re-export 出去，保留全项目既有的 import 路径 |
| `telegram_history.py` | Telegram 可见事件 → 对话历史的记录层，只写库并发信号 |
| `balance.py` / `stake_reward_pool.py` | 金币与奖池变动的唯一入口：带 op_key 的幂等操作和账本，契约见 [balance-service.md](balance-service.md) |
| `process_user.py` | 用户好感、印象、抽奖；旧的金币函数暂时保留并委托给余额服务（待移除） |
| `redaction.py` / `command_privacy.py` | 敏感数据脱敏策略的单一来源、凭据类命令的私聊限制，契约见 [sensitive-data.md](sensitive-data.md) |
| `telegram_utils.py` / `prompt_utils.py` / `token_estimator.py` / `archive_utils.py` / `command_cooldown.py` | 通用工具 |

`mysql_connection` 是 core → core 的转发，没有分层危害，长期保留即可；新代码可以直接 import 对应领域模块。

### features

| 目录 | 职责 |
|---|---|
| `conversation/` | AI 对话主路径，见下表 |
| `ai/` | 按 provider 声明表解析与调用模型（路由、聊天入口、task runner，契约见 [ai-provider-architecture.md](ai-provider-architecture.md)）、tools、summary、定时任务与 idle followup（claim 所有权、租约与崩溃恢复见 [job-recovery.md](job-recovery.md)）、翻译 handler、出站发送 |
| `profile/` | `/start` `/me` `/help` `/github` `/setmyinfo` 与入群欢迎 |
| `economy/` | 金币相关：`/lottery` `/give` `/rich`、商店、签到、质押、充值 |
| `crypto/` | 行情、图表、预测、swap，以及管理员的行情监控命令 |
| `admin/` | 开发者命令与 `/admin_announce` |
| `games/` `media/` `moderation/` | 玩法、媒体、群管 |

`features/conversation/` 内部：

| 文件 | 职责 |
|---|---|
| `handlers.py` | Telegram 入口：批处理窗口与会话锁、群聊触发与冷却判断、把 `Update` 映射成 `TurnRequest`；不含业务规则 |
| `turn.py` | 一轮对话的业务操作 `ConversationTurn`：扣费、历史、媒体、模型、投递的阶段，见「一轮对话」 |
| `turn_types.py` | 一轮对话的类型化边界：`TurnRequest`、`TurnResult`、`TurnTimings`、`ModelRequest` / `ModelResponse`、`ConversationSettings` |
| `turn_services.py` | 一轮对话依赖的外部能力（`TurnServices`）与生产实现 `default_services()` |
| `lifecycle.py` | `post_init` 与 bot 身份缓存 |
| `triggers.py` | 群聊里是否唤起 AI 的判断 |
| `batching.py` | 私聊连发消息的批处理窗口 |
| `billing.py` | 一轮对话的扣费：每条消息按持久身份记一笔账，整轮同一个事务，奖池贡献同事务；消息价格表 |
| `messages.py` | 消息 → AI 输入的整理与编辑去重 |
| `clear.py` | `/clear` |
| `history_hooks.py` | 注入 core 的历史回调，并注册历史入口 handler |

## 一轮对话

`ConversationTurn.run`（`features/conversation/turn.py`）按固定顺序执行 `turn_types.Stage` 里的阶段，
每个阶段的耗时记录在 `TurnResult.timings`，整轮结束时记一行 INFO 日志。排队（等会话锁）发生在进入之前，
由 `handlers._reply_locked` 测量后放进 `TurnRequest.queue_seconds`。

| 阶段 | 做什么 | 提前结束的状态（都已回复用户） |
|---|---|---|
| `plan` | 选出有内容的消息，按 `billing.text_message_cost` / `MEDIA_COST` 定价 | `NOTHING_TO_PROCESS`、`MESSAGE_TOO_LONG` |
| `charge` | 写完上一项历史事件，调用 `billing.charge_turn` 整轮扣费 | `UNREGISTERED`、`INSUFFICIENT_BALANCE` |
| `context` | 读取印象与日记，拼用户状态提示词 | |
| `prepare` | 逐条整理消息，下载并识别图片或贴纸 | `MEDIA_TOO_LARGE`、`MEDIA_FAILED` |
| `history_in` | 写入用户消息，读取历史 | |
| `model` | 输入状态、执行模型与工具、规范化回复 | |
| `history_out` | 写入工具结果与 AI 回复 | |
| `delivery` | 容量提示、最终回复、生成的媒体、群聊历史 | |
| `finalize` | AI 代执行 /clear 的归档、零余额边界写入 | |

事务所有权：

- 整轮唯一的跨语句事务在 `charge` 阶段：`billing.charge_turn` 里每条消息一笔账、奖池贡献在同一个事务中，
  余额不足整体回滚。它返回之后事务已经结束，后面的阶段都不持有事务。
- 历史写入（`history_in`、`history_out`、`finalize`）每次调用各自是 `core/chat_records.py` 里的一次短事务。
  扣费和历史不在同一个事务里，也没有补偿式退款。
- `model` 阶段不持有数据库事务；工具需要时自己开事务（走 [balance-service.md](balance-service.md) 的余额服务）。
- `delivery` 阶段只做 Telegram 发送与群聊历史记录。

模型执行只有一个调用点：`TurnServices.run_model`，输入输出是 `ModelRequest` / `ModelResponse`。
默认实现转给 `features/ai` 的 `get_ai_response`（路由、provider fallback、工具循环都在它后面）。
`model` 阶段在它外面设置「不记录 bot 自己发出的消息」的历史作用域，这属于一轮对话的历史语义，不属于模型执行。
`TurnServices` 的其余字段是历史读写、投递与媒体识别；测试用 `dataclasses.replace(default_services(), ...)`
替换需要的部分，不起 bot、不连数据库，见 `tests/test_conversation_turn.py`。

提前结束的状态里，`MEDIA_TOO_LARGE` 与 `MEDIA_FAILED` 发生在扣费之后，这一轮不退款。

## core 与业务之间的回调

`core.telegram_history` 只负责写库并发出信号，摘要生成、recap 失效与会话锁属于对话业务，
由 `features.conversation.history_hooks` 实现，`app` 在 `register_history_handlers` 时注入：

| 信号 | 触发时机 | 业务实现 |
|---|---|---|
| `on_history_overflow` | 活跃历史溢出 | 先尝试即时摘要，失败时退回后台排队 |
| `on_snapshot_created` | 新快照落库 | 后台排队生成摘要 |
| `private_command_guard` | 私聊命令进入 | 先让 recap 失效，再等待会话锁 |

未注入时 `core` 静默跳过对应动作。测试在 `tests/conftest.py` 里沿用同一份装配，
时序由 `tests/test_conversation_history_hooks.py` 钉住。

## 配置注入

配置由 `core/config.py` 的 `AppSettings`（pydantic-settings）承载，读取配置的代码在调用时访问
`config.<NAME>`。换配置就是重新发布这些模块级名字：

| 入口 | 作用 |
|---|---|
| `AppSettings.from_values(**values)` | 只用代码默认值与显式传入的值构造设置，不读 env 文件，也不读进程环境变量；未知名字报错 |
| `config.install_settings(settings)` | 让设置成为生效配置，重新发布全部模块级常量（含推导出来的 `AI_SERVICE_ORDER`、`SQLALCHEMY_DATABASE_URI` 等），返回之前的设置 |
| `config.use_settings(settings)` / `config.override_settings(**values)` | 上下文管理器：块内生效，退出时恢复；`override_settings` 等于 `use_settings(from_values(**values))` |
| `config.current_settings()` | 当前生效的设置对象 |
| `create_application(settings=None, *, bot=None)` | 应用组装的显式入口：传入的设置先成为生效配置；不传则用进程启动时从 `.env` 加载的那份 |

没有传入的值回到代码默认值，不沿用当前值：覆盖的结果不依赖开发者的 `.env` 或 shell。
安装配置会重写全部模块级常量，需要对个别常量再 monkeypatch 时，先装配置再 patch。

需要脱离全局配置的新代码，用「以 `config` 里的名字作为属性的对象」作为来源：`ai_providers` 的读取函数、
`provider_resolver`、`provider_params`、`run_chat_provider` 都有可选的 `settings` 参数，默认读 `core.config`；
对话入口读取的配置集中在 `ConversationSettings.from_config`。

测试从一份只含代码默认值的基线配置开始（`tests/conftest.py`），用 `settings_override` 夹具改配置，
细节见 [testing-guidelines.md](testing-guidelines.md)。

仍在导入时读取配置的模块不会跟着换配置：它们已经取走了旧值。完整清单是
`tests/test_config_injection.py` 的 `IMPORT_TIME_CONFIG_READS`，测试会拒绝新增的导入时读取。
迁移方式是把模块顶层的 `X = config.X` 改成在使用处读 `config.X`，迁完一个就从清单里删掉。
数据库引擎同样在首次使用时按当时的配置创建（`core/db.py` 的 `get_engine`），要在引擎创建之前装配置。

## 契约文档

| 文档 | 内容 |
|---|---|
| [balance-service.md](balance-service.md) | 金币与奖池变动的规则、`op_key`、账本 |
| [job-recovery.md](job-recovery.md) | 定时任务与空闲跟进的 claim、租约、阶段与恢复 |
| [sensitive-data.md](sensitive-data.md) | 脱敏策略的单一来源与覆盖路径 |
| [database-migrations.md](database-migrations.md) | 迁移的安装、升级、恢复与编写规则，MySQL 集成测试夹具 |
| [ai-provider-architecture.md](ai-provider-architecture.md) | provider 声明表、任务解析、fallback、协议要求 |
| [testing-guidelines.md](testing-guidelines.md) | 测试、类型检查与 CI |

## 回归锚点

`tests/test_handler_registry.py` 断言 handler 类型、group、命令名、callback `__name__` 与 job 的
`interval` / `first`，但不断言模块路径。搬家时只改 import，不改这些签名。

## 已知遗留

- `features/profile/handlers.py` 的 `/start` 直接 import `features.economy.ref` 处理推广邀请码，
  是目前唯一一处非 `conversation → ai` 的跨功能 import，待后续用启动参数回调解耦。
- `features/conversation` 依赖 `features/ai` 是有意为之：对话是 AI 业务的调用方，AI 不反向依赖对话。
- `features/conversation/triggers.py` 的 classifier 路径（`should_trigger_ai_response`）当前没有调用者，
  群聊触发只走「回复 bot」和「直接触发词」两条。代码保留待定。
- `features/games/rpg/` 的装备与道具子系统不可达：`rpg_equipment`、`rpg_items` 两张表没有种子数据，
  代码里也没有任何写入入口，因此 `/rpg equip` 和 `/rpg use` 永远找不到目标。
  `use_item` 更是只扣道具不产生效果（源码注释自陈「暂时留空」）。要启用得先补数据和效果实现。
