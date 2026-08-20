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
| `bot_app.py` | 构建 Application，挂 `post_init` / `post_stop` |
| `handler_registry.py` | 注册顺序的唯一来源（`REGISTRATION_STEPS`） |
| `handler_groups.py` | 按功能分组调用各 feature 的 `setup_*`，不实现业务 |
| `error_handler.py` | 全局错误回复，属于运行时而非某个功能 |

`register_core_command_handlers` 仍在组装层直接 `add_handler`：那一组命令的注册顺序在历史上跨功能交错，
而 `tests/test_handler_registry.py` 把最终顺序当作契约。要改成自注册必须先改这个契约。

### core（无业务）

| 文件 | 职责 |
|---|---|
| `config.py` / `db.py` / `bot_logging.py` | 配置、引擎、日志 |
| `sql.py` | 通用 SQL 助手：`fetch_one` / `fetch_all` / `execute` 与连接别名 |
| `chat_records.py` | AI 对话历史存储：写入、归档、裁剪、token 预算、history-state 事件 |
| `user_records.py` | user 表的基础查询 |
| `mysql_connection.py` | **兼容层**：把上面三者 re-export 出去，保留全项目既有的 import 路径 |
| `telegram_history.py` | Telegram 可见事件 → 对话历史的记录层，只写库并发信号 |
| `process_user.py` | 用户金币、好感、印象、抽奖 |
| `telegram_utils.py` / `prompt_utils.py` / `token_estimator.py` / `archive_utils.py` / `command_cooldown.py` | 通用工具 |

`mysql_connection` 是 core → core 的转发，没有分层危害，长期保留即可；新代码可以直接 import 对应领域模块。

### features

| 目录 | 职责 |
|---|---|
| `conversation/` | AI 对话主路径，见下表 |
| `ai/` | provider、task runner、tools、summary、idle followup、翻译 handler、出站发送 |
| `profile/` | `/start` `/me` `/help` `/github` `/setmyinfo` 与入群欢迎 |
| `economy/` | 金币相关：`/lottery` `/give` `/rich`、商店、签到、质押、充值 |
| `crypto/` | 行情、图表、预测、swap，以及管理员的行情监控命令 |
| `admin/` | 开发者命令与 `/admin_announce` |
| `games/` `media/` `moderation/` | 玩法、媒体、群管 |

`features/conversation/` 内部：

| 文件 | 职责 |
|---|---|
| `handlers.py` | Telegram 入口与一轮对话的编排 |
| `lifecycle.py` | `post_init` 与 bot 身份缓存 |
| `triggers.py` | 群聊里是否唤起 AI 的判断 |
| `batching.py` | 私聊连发消息的批处理窗口 |
| `messages.py` | 消息 → AI 输入的整理与编辑去重 |
| `clear.py` | `/clear` |
| `history_hooks.py` | 注入 core 的历史回调，并注册历史入口 handler |

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
