# 架构约定

## 依赖方向

```
app → features → core
app → core
```

- `src/fogmoe_telegram_bot/main.py` 只负责进程入口。
- `src/fogmoe_telegram_bot/app/` 负责组装 Telegram Application、注册 handler 和 job，并在启动时把业务回调注入 `core`。
- `src/fogmoe_telegram_bot/core/` 只放跨功能共享能力，禁止 import `features` 和 `app`。
- `src/fogmoe_telegram_bot/features/` 放业务功能。功能之间默认不互相 import；必须共享的能力抽到 `core`，或由 `app` 在启动时注入回调。

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
| `runtime_lifecycle.py` | 运行时的启动与关停顺序：准入、后台任务、HTTP 客户端、线程适配器、数据库引擎，见 [runtime.md](runtime.md) |

`register_core_command_handlers` 仍在组装层直接 `add_handler`：那一组命令的注册顺序在历史上跨功能交错，
而 `tests/test_handler_registry.py` 把最终顺序当作契约。要改成自注册必须先改这个契约。

### core（无业务）

| 文件 | 职责 |
|---|---|
| `config.py` / `db.py` / `bot_logging.py` | 配置、引擎、日志；配置的覆盖机制见「配置注入」 |
| `ai_providers.py` / `litellm_models.py` | AI provider 的唯一声明表，以及由它决定的名字与 LiteLLM 模型名转换，契约见 [ai-provider-architecture.md](ai-provider-architecture.md) |
| `sql.py` | 通用 SQL 助手：`fetch_one` / `fetch_all` / `execute` 与连接别名 |
| `chat_records.py` | AI 对话历史存储：写入、归档、裁剪、token 预算、history-state 事件 |
| `user_records.py` | user 表的基础读写：存在、余额（只读）、权限、用户名、按名字找 id、开户。跨功能共享的用户读取放这里，读取可传入调用方的 `connection` |
| `migration_support.py` | Alembic 迁移的支撑代码：数据库 URL 优先级、版本表宽度、可重入 DDL 助手，见 [database-migrations.md](database-migrations.md) |
| `mysql_connection.py` | **兼容层**：把上面三者 re-export 出去，保留全项目既有的 import 路径 |
| `telegram_history.py` | Telegram 可见事件 → 对话历史的记录层，只写库并发信号 |
| `balance.py` / `stake_reward_pool.py` | 金币与奖池变动的唯一入口：带 op_key 的幂等操作和账本，契约见 [balance-service.md](balance-service.md) |
| `process_user.py` | 用户好感与印象；`user_exists`、`get_user_coins`、`get_user_permission` 等历史入口只转发给 `user_records`。金币只走余额服务，旧的金币函数已移除 |
| `admission.py` / `deadline.py` | 对话轮次的准入控制（全局并发、每用户待处理数、有界排队）与整轮截止时间，见 [runtime.md](runtime.md) |
| `blocking.py` / `background.py` / `http_sessions.py` | 有界线程适配器（只给必须同步的代码用）、后台任务登记（关停时取消）、同步 HTTP 会话登记（关停时关闭） |
| `metrics.py` | 进程内指标（计数器、仪表、直方图）与周期汇总日志 |
| `redaction.py` / `command_privacy.py` | 敏感数据脱敏策略的单一来源、凭据类命令的私聊限制，契约见 [sensitive-data.md](sensitive-data.md) |
| `telegram_utils.py` / `prompt_utils.py` / `token_estimator.py` / `archive_utils.py` / `command_cooldown.py` | 通用工具 |

`mysql_connection` 是 core → core 的转发，没有分层危害，长期保留即可；新代码可以直接 import 对应领域模块。

### features

| 目录 | 职责 |
|---|---|
| `conversation/` | AI 对话主路径，见下表 |
| `ai/` | 按 provider 声明表解析与调用模型（路由、聊天入口、task runner，契约见 [ai-provider-architecture.md](ai-provider-architecture.md)）、tools、summary、定时任务与 idle followup（claim 所有权、租约与崩溃恢复见 [job-recovery.md](job-recovery.md)）、翻译 handler、出站发送 |
| `profile/` | `/start` `/me` `/help` `/github` `/setmyinfo` 与入群欢迎 |
| `economy/` | 金币相关：`/lottery` `/give` `/rich`、商店、签到、质押、充值、邀请、任务、网页密码；按适配层、`operations/`、`repositories/` 分层，见「经济与游戏的分层」 |
| `crypto/` | 行情、图表、预测、swap，以及管理员的行情监控命令；预测与 swap 的 SQL 在 `crypto/repositories/` |
| `admin/` | 开发者命令与 `/admin_announce` |
| `games/` `media/` `moderation/` | 玩法、媒体、群管；游戏里持有金币的状态（下注轮次、石头剪刀布对局）持久化在 MySQL，恢复策略见 [job-recovery.md](job-recovery.md) 的「游戏状态」；`games/` 的 SQL 都在 `games/repositories/`，见「经济与游戏的分层」 |

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
每个阶段的耗时记录在 `TurnResult.timings`，整轮结束时记一行 INFO 日志并写入指标。排队（每用户待处理数、会话锁、全局槽位）
发生在进入之前，由 `handlers` 测量后放进 `TurnRequest.queue_seconds`；排队被拒绝时这一轮没有开始，也就没有扣费。
整轮截止时间 `TurnRequest.deadline` 从进入队列开始计时，覆盖排队、`prepare`、`model` 与 `delivery`，规则见 [runtime.md](runtime.md)。

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
默认实现转给 `features/ai` 的 `get_ai_response`（路由、provider fallback、工具循环都在它后面），它是原生 async：
模型调用 `litellm.acompletion`，async 工具直接 `await`，同步工具走有界线程适配器，没有「事件循环 → 线程 → 事件循环」的往返。
`model` 阶段在它外面设置「不记录 bot 自己发出的消息」的历史作用域，这属于一轮对话的历史语义，不属于模型执行。
`TurnServices` 的其余字段是历史读写、投递与媒体识别；测试用 `dataclasses.replace(default_services(), ...)`
替换需要的部分，不起 bot、不连数据库，见 `tests/test_conversation_turn.py`。

提前结束的状态里，`MEDIA_TOO_LARGE`、`MEDIA_FAILED` 与 `DEADLINE_EXCEEDED`（模型之前的阶段截止时间到期）发生在扣费之后，这一轮不退款。
这是已确认的产品规则：扣费之后的失败与超时都不退款。
模型阶段与投递阶段到期不是提前结束：回复固定的超时提示，历史与收尾照常进行，同样不退款。

## 经济与游戏的分层

`features/economy/` 与 `features/games/` 按「传输 / 业务操作 / 持久化」三层组织，依赖只能向下：

```
适配层（Telegram handler） → 业务操作 → repository → core.sql
                                  └──→ core.balance / core.stake_reward_pool（金币与奖池的唯一变动入口）
```

| 层 | 位置 | 做什么 | 不做什么 |
|---|---|---|---|
| 适配层 | `economy/` 下的 `shop.py`、`stake_coin.py`、`charge_coin.py`、`coins.py`、`checkin.py`、`task.py`、`ref.py`、`web_password.py`、`bribe.py`；`games/` 下的 `gamble.py`、`rockpaperscissors_game.py`、`omikuji.py`、`sicbo.py`、`rpg/commands.py` 等 | 解析 `Update` 与按钮回调数据，组装业务操作的参数，把结果映射成回复、编辑、通知；handler 与 job 的注册（名字与顺序是 `tests/test_handler_registry.py` 的契约）；进程内的节流 | 不写 SQL，不开事务，不直接调用 repository，不决定扣多少钱 |
| 展示 | `economy/shop_views.py` | 商店的菜单、按钮回调数据与解析（`parse_callback`）、购买结果的文案 | 不碰数据库 |
| 业务操作 | `economy/operations/*.py`；`games/gamble_rounds.py`、`rps_games.py`、`rpg/settlement.py` | 规则、事务、`op_key`、余额变动。输入是类型化的请求或普通参数，结果是 dataclass 与枚举（`PurchaseStatus`、`GiveStatus`、`CheckinOutcome`、`RedeemResult` 等） | 不 import `telegram`；economy 的操作不返回用户文案，由适配层与展示层生成 |
| repository | `economy/repositories/*.py`、`games/repositories/*.py`；跨功能共享的用户读取在 `core/user_records.py` | 单条语句级别的读写（SQLAlchemy Core 的 `exec_driver_sql` 与 `core.sql`），返回 dataclass 或基础类型，并定义持久化的状态取值与记录类型 | 不开事务、不提交，不含业务规则，不 import `telegram`、`core.balance`、`core.stake_reward_pool` |

事务所有权：

- **事务由业务操作持有。** 改动余额的操作用 `balance.run_in_transaction(work)`（死锁时整个事务重跑，所以 `work` 里
  不做事务之外的副作用），签到、抽奖、任务、卡密兑换、充值请求沿用 `sql.transaction()`；余额变动与业务状态在同一个事务里提交或回滚，
  所以失败不需要退款，规则见 [balance-service.md](balance-service.md) 的「事务所有权」。
- **repository 只接受 `connection`。** 写入函数必须传入调用方事务的 `connection`；读取函数的 `connection` 可选，
  传入时在调用方的事务里读（拿到 user 行锁之后的第一次一致性读才能看到上一个持锁者提交的值），不传时用一次性连接。
  加锁读取（`for_update=True`、`lock_*`）的顺序由操作决定：user 行先于奖池行，游戏轮次/对局行先于 user 行。
- **事务里不发消息。** 适配层在操作返回（事务已经提交）之后才回复、编辑面板或通知；商店的保底计数这类进程内状态也只在提交之后更新。
- 内置了「进程内」保护的地方保持原样：商店的购买锁（保护保底计数）、抽奖与卡密兑换的进程内「处理中」标记；
  数据库层的串行始终靠 user 行锁与唯一键，不依赖它们。

经济功能的业务操作与 repository：

| 功能 | 业务操作（`economy/operations/`） | repository（`economy/repositories/`） |
|---|---|---|
| 商店 | `shop.py`：`buy_memory_limit`、`upgrade_permission`、`buy_scratch_ticket`、`buy_huanle_ticket`，每种商品一个请求与结果类型（`MemoryLimitPurchase` / `MemoryLimitResult`、`PermissionUpgrade` / `PermissionUpgradeResult`、`TicketPurchase` / `TicketResult`）；权限升级规则、开奖概率与保底计数（`advance_pity`）也在这里 | `shop.py`：永久记忆上限与权限等级 |
| 质押 | `stake.py`：`open_stake`、`collect_stake_reward`、`withdraw_stake_principal`，回报率与回报周期的计算规则 | `stake.py`：`user_stakes` 与回报率用的金币、质押总量 |
| 赠送与富豪榜 | `coins.py`：`transfer_coins`、`richest_users`，手续费与每日次数上限 | `coins.py`：`user_give_daily`、富豪榜 |
| 签到 | `checkin.py`：`process_checkin`，连续天数与奖励档位 | `checkin.py`：`user_checkin` |
| 抽奖 | `lottery.py`：`lottery`、`async_lottery`，奖励档位与 24 小时冷却 | `lottery.py`：`user_lottery` |
| 邀请 | `invitations.py`：`add_invitation_record`、邀请信息查询 | `invitations.py`：`user_invitations` |
| 任务 | `task.py`：`claim_task_reward`、任务定义 | `tasks.py`：`user_task` |
| 充值 | `charge.py`：`redeem_code`、`decide_topup_request`、`create_topup_request`、`generate_codes` | `charge.py`：`redemption_codes`、`topup_requests`、`/recharge` 禁用截止时间 |
| 网页密码 | `web_password.py`：格式校验、Argon2id 哈希与校验、`process_set_web_password` | `web_passwords.py`：`web_password` |
| 贿赂 | `bribe.py`：`pay_bribe`（命令当前禁用） | 好感度在 `core/process_user.py` |

游戏沿用 D2 的持久化业务模块，SQL 收拢到 `games/repositories/`：

| 玩法 | 业务操作 | repository |
|---|---|---|
| 多人下注 | `gamble_rounds.py`：开局、接受下注、结算、恢复、公告 | `gamble.py`：`gamble_rounds`、`gamble_bets`，`Round` / `Bet` 与状态取值 |
| 石头剪刀布 | `rps_games.py`：建局与入场扣款、选择、超时、取消、恢复 | `rps.py`：`rps_games`，`Game` / `Seat` 与状态取值 |
| 御神签 | `omikuji.py` 的 `draw_daily_fortune`（很短，仍与适配层同文件） | `omikuji.py`：`user_omikuji` |
| RPG | `rpg/settlement.py`：回血、击败怪物、玩家对战的结算 | `rpg.py`：角色、装备、道具、战斗经验 |

加密货币里持有金币的两个入口也一样收拢了 SQL（操作仍与 Telegram 适配层在同一个模块里，`features/crypto/repositories/`）：
BTC 价格预测（`crypto_predict.py` 的 `create_prediction`、`check_prediction_result`）用 `predictions.py`，`$FOGMOE` 兑换
（`swap_fogmoe_solana_token.py` 的 `submit_swap_request`）用 `swaps.py`。

新增或修改一个会改余额的操作时：SQL 写进对应 repository；操作持有事务并把余额变动放进去，`op_key` 登记到
[balance-service.md](balance-service.md)；适配层只做映射；新的操作与 repository 模块加入 `pyproject.toml` 的 mypy `files`。
`tests/test_persistence_boundary.py` 用 AST 检查适配层与操作里没有 SQL、repository 不持有事务也不含业务，违反时会失败。
各层的测试方式见 [testing-guidelines.md](testing-guidelines.md) 的「经济与游戏的测试」。

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
economy 的 `ADMIN_USER_ID`（`charge_coin.py`）与 `NEW_USER_BONUS_COINS`（邀请奖励的总额由 `invited_user_reward()` 在调用时算出）已经迁完。
数据库引擎同样在首次使用时按当时的配置创建（`core/db.py` 的 `get_engine`），要在引擎创建之前装配置。

## 契约文档

| 文档 | 内容 |
|---|---|
| [balance-service.md](balance-service.md) | 金币与奖池变动的规则、`op_key`、账本 |
| [runtime.md](runtime.md) | 执行模型、准入与整轮截止时间、线程适配器清单、取消与关停顺序、指标与基准 |
| [job-recovery.md](job-recovery.md) | 定时任务与空闲跟进的 claim、租约、阶段与恢复 |
| [sensitive-data.md](sensitive-data.md) | 脱敏策略的单一来源与覆盖路径 |
| [database-migrations.md](database-migrations.md) | 迁移的安装、升级、恢复与编写规则，MySQL 集成测试夹具 |
| [ai-provider-architecture.md](ai-provider-architecture.md) | provider 声明表、任务解析、fallback、协议要求 |
| [testing-guidelines.md](testing-guidelines.md) | 测试、类型检查与 CI |

## 回归锚点

`tests/test_handler_registry.py` 断言 handler 类型、group、命令名、callback `__name__` 与 job 的
`interval` / `first`，但不断言模块路径。搬家时只改 import，不改这些签名。

## 已知遗留

- 经济、游戏与加密货币的两个金币入口之外，仍有 SQL 留在 handler 或共享模块里：`features/crypto/chart.py`、
  `features/profile/handlers.py`（`/me` 开户与个人信息）、`features/ai/tools/`、`features/moderation/`，以及 `core/process_user.py`
  里的好感度与印象。其中涉及金币的（`/me` 的开户奖励、AI 善意赠币）变动已经走余额服务，但 SQL 还没有收拢到 repository。
- `features/games/rpg/` 的 `characters.py` 与 `equipment/` 仍把操作、事务和给用户的文案混在同一批函数里（SQL 已在 `games/repositories/rpg.py`，
  金币相关的结算已经独立在 `rpg/settlement.py`）。
- `features/profile/handlers.py` 的 `/start` 直接 import `features.economy.ref` 处理推广邀请码，
  是目前唯一一处非 `conversation → ai` 的跨功能 import，待后续用启动参数回调解耦。
- `features/conversation` 依赖 `features/ai` 是有意为之：对话是 AI 业务的调用方，AI 不反向依赖对话。
- `features/conversation/triggers.py` 的 classifier 路径（`should_trigger_ai_response`）当前没有调用者，
  群聊触发只走「回复 bot」和「直接触发词」两条。代码保留待定。
- `features/games/rpg/` 的装备与道具子系统不可达：`rpg_equipment`、`rpg_items` 两张表没有种子数据，
  代码里也没有任何写入入口，因此 `/rpg equip` 和 `/rpg use` 永远找不到目标。
  `use_item` 更是只扣道具不产生效果（源码注释自陈「暂时留空」）。要启用得先补数据和效果实现。
