# 定时任务与空闲跟进的恢复

本页是 AI 定时任务（`features/ai/scheduler.py`）和空闲跟进（`features/ai/idle_followup.py`）在进程崩溃、卡死、重启之后如何恢复的契约，也是运维排查卡住任务的手册。共用部分在 `features/ai/job_claims.py`；表结构在 `alembic/versions/0021_job_claims.py`。

范围只有这两个后台任务。游戏状态的持久化不在这里。

## 所有权

一次 claim 就是一个 worker 对一个任务的独占处理权，由三样东西组成：

- **`claim_token`**：每次 claim 随机生成的 32 位十六进制串（`job_claims.new_claim_token`）。回收之后再次 claim 一定是新的 token。
- **`claim_until`**：租约到期时间，用数据库时钟（`UTC_TIMESTAMP()`）写入和比较，多个进程之间没有时钟偏差。
- **`stage`**：执行阶段，见下文。

规则：

1. 所有状态转换和完成写入都带 `WHERE ... AND claim_token = %s AND status = 'executing'`。空闲跟进还带 `activity_version`。影响行数为 0 就是失去所有权，抛 `ClaimLostError`，worker 不再写这个任务，也不再产生任何后续副作用。
2. 阶段转换要求租约仍然有效（`claim_until > UTC_TIMESTAMP()`）：租约都确认不了的 worker 不应该开始产生副作用。续期和完成写入只看 token。
3. 用户有了新活动（`note_incoming_private_message`、`arm_from_private_turn`、`/clear` 删除跟进）会改写或删除跟进行并清掉 token，旧 claim 因此失效，与回收导致的失效走同一条路径。
4. `schedule_ai_message_tool` 复用旧行时会清掉 claim 字段，并且不会复用 `executing` 的行。

### 一次只 claim 一个

定时任务每次轮询最多处理 `SCHEDULE_BATCH_SIZE` 个，但逐个 claim、逐个处理。崩溃最多卡住当前这一个，同批其他任务仍是 `pending`，下一次轮询直接处理，不用等租约到期。`SKIP LOCKED` 让多个进程互不阻塞。

空闲跟进一次 claim 一批（`IDLE_FOLLOWUP_BATCH_SIZE`），但批内的 claim 同时开始处理、各自有心跳，没有「已 claim 却没开始」的任务。

### 租约、续期与执行上限

`job_claims.run_leased` 在 worker 执行期间做三件事：

- **续期**：每隔 `*_HEARTBEAT_SECONDS` 延长一次租约，租约长度是 `*_LEASE_SECONDS`，心跳必须明显短于租约。
- **失去所有权就取消**：续期被拒绝（token 已变、状态已变）、或者连续无法确认租约满一个租约长度（数据库不可达），worker 被取消并置位 `abort_event`。续期遇到的单次数据库异常只记日志，等下一拍。
- **执行上限**：超过 `*_EXECUTION_TIMEOUT_SECONDS` 取消 worker。此时 claim 仍属于本 worker，按失败所处阶段收尾，见「按阶段的恢复策略」。

`abort_event` 是 `threading.Event`，放在 `tool_context[ABORT_EVENT_KEY]`。线程池里的工具循环（`tool_runner.run_tool_loop`）在每一轮模型调用前和每个工具执行前检查它，`TelegramVisibleContentHandler` 在每次发送前检查它；置位后抛 `JobAbortedError`。它继承 `BaseException`，不会被 router 当成 provider 失败去累计熔断或换 provider 重跑。已经在执行的单个工具无法撤回。

常量名：`SCHEDULE_LEASE_SECONDS`、`SCHEDULE_HEARTBEAT_SECONDS`、`SCHEDULE_EXECUTION_TIMEOUT_SECONDS`、`SCHEDULE_MAX_CLAIM_ATTEMPTS`、`IDLE_FOLLOWUP_LEASE_SECONDS`、`IDLE_FOLLOWUP_HEARTBEAT_SECONDS`、`IDLE_FOLLOWUP_EXECUTION_TIMEOUT_SECONDS`、`IDLE_FOLLOWUP_MAX_CLAIM_ATTEMPTS`，默认值取自 `job_claims.DEFAULT_*`。

## 阶段

| 阶段 | 进入时机 | 可能已有的副作用 |
|---|---|---|
| `claimed` | claim 时 | 无。只做只读检查；空闲跟进的回顾生成也在这里，它只用只读工具 |
| `generating` | 模型与工具开始之前 | 工具效果、已经发出的可见消息、定时任务的触发消息写入对话历史 |
| `delivering` | 模型与工具结束之后、写历史和发送之前 | 对话历史、Telegram 消息、媒体 |
| `completed` | 只出现在尝试记录里 | — |

每次转换和对应的数据库写入在同一个事务里提交：

- 定时任务 `claimed -> generating`：占用当日触发额度（`reserve_daily_schedule_trigger`）、阶段转换、尝试记录的 `daily_trigger_reserved`。额度已满时同一个事务把任务放回 `pending`，什么都不占用。
- 终结（`executed` / `failed` / 循环任务改期 / 放回队列）：任务行的状态与尝试记录的结果。

## 按阶段的恢复策略

租约到期的 `executing` 行由每次轮询开头的回收（`_recover_expired_schedules`、`_recover_expired_followups`）处理：

| 崩溃时的阶段 | 策略 | 尝试记录的结果 |
|---|---|---|
| `claimed` | 放回队列，下一次 claim 用新 token 重跑。同一次运行被 claim 的次数达到上限后放弃，任务按失败收尾 | `expired`；放弃时 `abandoned` |
| `generating` | 不重跑整轮。外部结果未知，任务按失败收尾 | `unknown` |
| `delivering` | 不重发。外部结果未知，任务按失败收尾 | `unknown` |

「按失败收尾」：一次性任务记为 `failed`，空闲跟进记为 `fired`，原因写进 `error` / `last_error`；循环任务的处理见下文。

**为什么 `generating` 不重跑整轮。** 工具结果只在 `tool_runner` 的内存里累积，整轮结束后才写入对话历史（`tool_logs_to_record_entries`），中途没有可以恢复的点。崩溃之后无法知道哪些工具已经完成，重跑会重放有副作用的工具（写日记、建任务、生图、命令）和已经发出的消息。所以不从中间恢复，也不重跑整轮。

**确认完成与结果未知的区别。** 只有终结事务提交了，才算确认完成（`completed`）。任何在 `generating` 或 `delivering` 阶段被回收的 claim 都记为 `unknown`，包括 Telegram 已经接受了消息、本地还没来得及记录的情况。

### 已知失败的重试

进程还在、操作抛出了异常（已知失败）时：

- 定时任务：`claimed` 阶段的失败放回队列重试，次数受 `SCHEDULE_MAX_CLAIM_ATTEMPTS` 限制，本轮轮询不会再次 claim 同一个任务；其余阶段按失败收尾。
- 空闲跟进：失败发生在 `claimed` 阶段，或者主模型明确失败且没有执行过任何工具（`_NoSideEffectsError`），延迟 `IDLE_FOLLOWUP_RETRY_MINUTES` 后重试，次数受 `IDLE_FOLLOWUP_MAX_RETRIES` 限制；其余失败发生在工具或投递可能已经产生副作用之后，直接结束（`fired`），不重跑。

### 循环任务

无论本次结果是成功、失败还是结果未知，下一次运行时间只推进一次：由持有 claim 的 worker 的终结事务，或者回收，二选一完成。因为两者都带 token 条件，旧 worker 迟到的写入被拒绝，不会推进第二次。

推进的基准是本次运行的 `run_at`（`_calculate_next_run_at`），写入 `last_run_at`；失败原因保留在 `error`，任务仍是 `pending`。只有永久性失败（用户不存在）才记为 `failed`。

### 用户回来时的空闲跟进

用户的新活动让 claim 失效之后：

- 心跳在下一拍发现 token 已被清掉，取消 worker，不再继续发送；用户的新消息不会被过期的跟进长时间占着会话锁。
- 如果模型与工具在心跳发现之前就结束了，而且已经执行过工具，这一轮的历史（回顾事件和工具结果）仍会写入，让模型知道工具做过什么，但不发送给用户。没有执行过工具就什么都不写。

## Telegram 投递的重复与丢失

`sendMessage` 不支持幂等键，Bot API 也没有「这条消息是否已经发出」的查询，无法对账。策略是 **at-most-once**：投递阶段结果未知时不重发。

| | 做法 | 代价 |
|---|---|---|
| 丢失 | 进程在 `generating` / `delivering` 阶段崩溃，用户可能收不到这次提醒或跟进 | 一次性提醒会丢；任务带着原因记为 `failed`，运维可以手动重新排队 |
| 重复 | 同一次 claim 不会因为恢复而重发；模型文本本身不确定，重发也不会得到同一条消息 | 同一轮里已经发出的消息（`generating` 阶段的可见内容、分段发送的前几段）不会撤回 |

也就是说：宁可丢，不重复。对账手段只有人工：用 `ai_job_attempts` 里的阶段判断崩溃时到了哪一步，再看用户的聊天记录确认是否送达。

## 关停

- PTB 停止时 `Application.running` 变为 `False`。两个轮询在 claim 新任务之前检查它，已经开始停止就不再 claim。
- 已经 claim、还停在 `claimed` 阶段的任务（比如在等会话锁），在拿到锁之后检查到应用正在停止，立即放回队列并写 `released`，不计为一次尝试。worker 被取消时同样释放 `claimed` 阶段的 claim。
- 已经进入 `generating` / `delivering` 的任务不强求优雅完成。PTB 会等正在执行的轮询结束，进程被强制终止时 claim 留在库里，租约到期后下一次启动按上面的策略回收。
- 租约到期之前任务不会被别的轮询接手，所以崩溃或强杀之后，任务最迟在一个租约长度加一个轮询间隔之后被回收。

## 数据

`ai_schedules` 和 `ai_idle_followups` 的 `claim_token`、`claim_attempts`（同一次运行已被 claim 的次数，终结或暂停时清零）、`stage`；`ai_schedules.claim_until`（空闲跟进原本就有）。

`ai_job_attempts` 每次 claim 一行：`job_type`（`schedule` / `idle_followup`）、`job_id`（任务 id 或 user_id）、`job_version`（空闲跟进的 `activity_version`）、`claim_token`（唯一，是这次尝试的稳定标识）、`attempt_no`、`stage`、`outcome`、`daily_trigger_reserved`、`error`（经 `describe_exception` 脱敏）、时间戳。`outcome` 为空表示进行中。结果的取值是 `job_claims.OUTCOME_*`：

| outcome | 含义 |
|---|---|
| `completed` | 本地确认完成 |
| `failed` | 已知失败，不再重试 |
| `retry` | 已知失败且没有副作用，放回队列 |
| `paused` | 余额不足或当日额度已满，放回队列 |
| `released` | 进程停止，尚未开始就释放 |
| `expired` | 租约在 `claimed` 阶段到期，放回队列 |
| `unknown` | 租约在 `generating` / `delivering` 阶段到期，结果未知，不重跑 |
| `abandoned` | `claimed` 阶段反复到期，超过上限 |
| `superseded` | claim 已不在任何任务行上（用户有了新活动、任务行被替换或删除），由轮询清扫关闭 |

已完成的记录在 `ATTEMPT_RETENTION_DAYS` 天后由轮询顺带清理。

旧版本留下的 `executing` 行（没有 token 和租约）由迁移标记为 `generating` 并让租约立即到期，第一次轮询按结果未知收尾，不会重跑。

## 排查与手动处理

卡住的任务看 `status = 'executing'`：

```sql
-- 定时任务：正在执行或租约已过期还没被回收的
SELECT id, user_id, stage, claim_attempts, claim_until, claim_until <= UTC_TIMESTAMP() AS expired
FROM ai_schedules WHERE status = 'executing';

-- 空闲跟进
SELECT user_id, stage, claim_attempts, claim_until FROM ai_idle_followups WHERE status = 'executing';

-- 某个任务的每次尝试
SELECT * FROM ai_job_attempts WHERE job_type = 'schedule' AND job_id = 42 ORDER BY id;

-- 最近结果未知的尝试
SELECT job_type, job_id, stage, error, stage_at FROM ai_job_attempts
WHERE outcome IN ('unknown', 'abandoned') ORDER BY id DESC LIMIT 50;
```

处理：

- **租约过期但还没被回收**：不用手动处理，下一次轮询（`SCHEDULE_POLL_INTERVAL` / `IDLE_FOLLOWUP_POLL_INTERVAL`）会按阶段回收。
- **不想等租约**：确认进程已经停止，再让租约立刻到期，`UPDATE ai_schedules SET claim_until = UTC_TIMESTAMP() WHERE id = 42 AND status = 'executing'`（空闲跟进同理，按 `user_id`）。仍按阶段回收，`generating` / `delivering` 不会因此重跑。
- **结果未知的一次性提醒要补发**：先确认用户确实没有收到，再重新排队。`UPDATE ai_schedules SET status = 'pending', run_at = UTC_TIMESTAMP(), error = NULL, claim_attempts = 0 WHERE id = 42 AND status = 'failed'`。这会让模型重新生成并发送一次。
- **不要**直接把 `executing` 的行改成 `pending` 并保留 token：旧 worker 如果还活着，它的写入仍会通过。要改就同时清掉 `claim_token`、`claim_until`，并把 `stage` 设为 `idle`。

## 变更检查

- 新增会产生副作用的步骤：想清楚它属于哪个阶段；在阶段转换之前不能有任何外部副作用。
- 新增任务状态写入：必须带 `claim_token` 条件，影响行数为 0 时抛 `ClaimLostError`，并且和对应的尝试记录在同一个事务里。
- 新增会改变「哪些阶段可以安全重跑」的行为，同步更新「按阶段的恢复策略」。
- 测试：`tests/integration/test_schedule_recovery.py`、`tests/integration/test_idle_followup_recovery.py` 覆盖崩溃、回收、旧 worker 迟到、租约；用法见 [database-migrations.md](database-migrations.md) 的「运行集成测试」。
