# 定时任务与空闲跟进的恢复

本页是 AI 定时任务（`features/ai/scheduler.py`）和空闲跟进（`features/ai/idle_followup.py`）在进程崩溃、卡死、重启之后如何恢复的契约，也是运维排查卡住任务的手册。共用部分在 `features/ai/job_claims.py`；表结构在 `alembic/versions/0020_job_claims.py`。

范围是这两个后台任务。持有金币的游戏（多人下注、石头剪刀布）的状态持久化与重启恢复在文末「游戏状态」。

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

`abort_event` 是 `threading.Event`，放在 `tool_context[ABORT_EVENT_KEY]`。工具循环（`tool_runner.run_tool_loop`）在每一轮模型调用前和每个工具执行前检查它，`TelegramVisibleContentHandler` 在每次发送前检查它；置位后抛 `JobAbortedError`。它继承 `BaseException`，不会被 router 当成 provider 失败去累计熔断或换 provider 重跑。

工具循环与可见内容发送都跑在事件循环里（原生 async，见 [runtime.md](runtime.md)），所以 `run_leased` 取消 worker 时，正在等待的模型调用与 async 工具会被**直接取消**，不必等到下一个检查点；`abort_event` 继续在每个检查点（模型调用前、工具执行前、每次发送前，包括发送准备期间的再次检查）阻止后续动作。已经在线程里执行的同步工具不检查它，无法撤回，线程自己跑完、结果被丢弃。

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
- 进程停止时的整体顺序（准入关闭、后台任务取消、HTTP 客户端与线程适配器关闭、数据库引擎 dispose）见 [runtime.md](runtime.md) 的「取消与关停」；上面这几条 claim 语义不受它影响。

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

## 游戏状态

范围是持有金币的两个游戏：多人下注（`features/games/gamble.py`，业务在 `gamble_rounds.py`）和
石头剪刀布（`rockpaperscissors_game.py`，业务在 `rps_games.py`）。表由迁移 `0021_game_state` 创建：
`gamble_rounds`、`gamble_bets`、`rps_games`。骰宝、御神签与 RPG 不跨请求持有金币：每次点击或命令在
一个事务里扣款、结算、更新状态，没有需要恢复的中间状态，见 [balance-service.md](balance-service.md)。

### 原则

- 状态在 MySQL 里，进程重启不丢。内存里只剩两样不含金币的东西：石头剪刀布的等待房间（有人加入、
  对局创建的那一刻才扣入场费）和下注面板的编辑锁。
- 余额变动与状态转换在同一个事务里提交；转换只发生一次：先 `SELECT ... FOR UPDATE` 锁轮次/对局行，
  状态必须仍是进行中。余额操作的 op_key 由轮次/对局 id 派生，见 [balance-service.md](balance-service.md)。
- 事务里不发消息。面板编辑在提交之后进行，是尽力而为：表里的 `announced_at` 记录结果是否已经写到面板上，
  没写成功的由恢复任务补发（编辑是幂等的，Telegram 回「内容没变」「消息不存在」这类明确拒绝视为完成，
  网络错误和限流留待下次），补发窗口 1 天。
- 到期靠两层保证：每局创建时用 `job_queue.run_once` 安排一次精确的定时器，另有周期的恢复任务
  （`recover_gamble_rounds`、`recover_rps_games`，启动后 5 秒第一次运行，之后每 30 秒）。定时器随进程
  丢失也没关系，恢复任务处理所有已到期的局。截止时间一律用数据库时钟比较。

### 多人下注

| 状态 | 含义 |
|---|---|
| `open` | 接受下注。`active_slot = 1`，唯一键保证同一时间只有一个开放轮次 |
| `settled` | 已结算：有人下注就同事务把整个奖池入账给抽中的人，没人下注就只是关闭 |
| `refunded` | 抽中的人账户已不存在，全额退回各人的下注 |
| `cancelled` | 面板没能发出去，没有人下过注 |

- 按钮的 `callback_data` 是 `gamble_<round_id>_<amount>`。回调校验轮次存在、仍然 `open` 且没过截止时间、
  点击的消息与轮次记录的 chat/message 一致；旧格式（只有金额）、旧轮次、其他面板一律拒绝，不扣款。
  轮次创建时还没有 message_id，面板发出去之后才登记，在那之前所有下注都被拒绝。
- 接受下注：一个事务里锁轮次行、确认 `open`、插入下注（`(round_id, user_id)` 唯一）、`balance.debit`。
  任何一步失败整体回滚，扣款期间轮次被结算抢先拿到锁时，下注被拒绝，不扣款。
- 结算：同样先锁轮次行，所以与下注串行；按下注金额为权重抽中奖者，`balance.credit` 奖金，
  状态从 `open` 转换一次。
- 恢复策略：**已过截止时间的轮次照常结算**，没过的继续开放。理由：下注是在「5 分钟后开奖」的规则下接受的，
  抽取不依赖任何进程内状态，重启只是让开奖晚了一点；全额退款会让已经下注的人失去这一局，
  而且退款本身并不比结算更安全。
  - 结算提交之后、进程在编辑面板之前退出：轮次是 `settled` 而 `announced_at` 为空，恢复任务补发结果。
  - 轮次有记录而面板没有登记（进程在发面板前后退出）：没人能下注，到期后按「无人参与」关闭。
  - 结算事务失败（数据库异常）：整体回滚，轮次仍是 `open`，恢复任务下一轮重试；
    日志里有「结算轮次 N 失败」。
- 新的 `/gamble` 命令在开局前会先结算已到期的旧轮次，所以重启后不用等恢复任务就能开新局。

### 石头剪刀布

| 状态 | 结果 | 含义 |
|---|---|---|
| `choosing` | | 进行中 |
| `settled` | `p1` / `p2` | 胜负已分，奖金 `rps:<id>:win` 已入账 |
| `settled` | `draw` | 平局，入场费已退回 |
| `refunded` | `timeout` | 过期仍未分出胜负，入场费已退回 |
| `refunded` | `failed` | 创建后面板发不出去（或胜者账户已不存在），入场费已退回 |

- 两名玩家的入场扣款与对局创建在**同一个事务**：先按 id 升序锁两个 user 行，确认两人都不在进行中的对局里，
  插入对局，再扣款。任何一方余额不足或用户不存在，整个事务回滚，没有对局，另一方也没有被扣款。
- 退款一律 `balance.refund(rps:<id>:entry:<uid>)`，所以平局、超时、创建失败都只会退一次；
  每次转换先锁对局行，状态必须仍是 `choosing`。
- 选择按钮是 `rps_choice_<game_id>_<choice>_<uid>`，绑定对局和玩家。选择与结算在同一个事务里：
  第二个人选择时，同一个事务里算出胜负、入账奖金（平局退款）并终结对局。
  选择时已过 `expires_at` 的，直接按超时退款。
- 恢复策略：**已过期的对局退款，没过期的继续**。选择记在表里，按钮仍然有效，重启不会打断一局进行中的游戏。
  - 对局提交之后、面板发出之前进程退出：没人能选择，2 分钟后恢复任务退款。
  - 创建对局之后发面板失败（比如玩家从没私聊过机器人）：当场 `cancel_game` 退款，并告诉发起加入的人创建失败。
  - 终结之后、进程在编辑面板之前退出：`announced_at` 为空，恢复任务补发；玩家点旧按钮时也会触发补发。
- 等待房间不放金币，只在内存里：重启丢失的只是一张邀请，旧邀请上的「加入」按钮提示「已开始或已被取消」。
  加入按钮必须出现在等待房间自己的那条消息上，已取消、已过期邀请的按钮不会加入当前房间。

### 升级时正在进行的游戏

旧版本把轮次和对局放在内存里，扣款用旧的金币接口（账本里 `reason` 以 `legacy:` 开头）。升级重启时：

- 进行中的下注轮次和对局随旧进程消失，**已扣的金币不会自动退还**。升级尽量选在没有进行中的 `/gamble`
  和 `/rps_game` 时；否则事后用 `coin_ledger` 里升级前后的 `legacy:spend_user_coins` 记录人工补偿。
- 旧版本发出的下注按钮（只有金额）和选择按钮（没有对局 id）一律被拒绝，不扣款。

已终结的轮次与对局留在表里，是账本记录的业务依据；每局一行，数据量很小，暂不清理。

### 排查

```sql
-- 开放的轮次、参与人数与奖池
SELECT r.id, r.status, r.closes_at, COUNT(b.id) AS bets, COALESCE(SUM(b.amount), 0) AS pool
FROM gamble_rounds r LEFT JOIN gamble_bets b ON b.round_id = r.id
WHERE r.status = 'open' GROUP BY r.id;

-- 已终结但结果还没写到面板上的
SELECT id, status, settled_at FROM gamble_rounds WHERE status <> 'open' AND announced_at IS NULL;
SELECT id, status, outcome, finished_at FROM rps_games WHERE status <> 'choosing' AND announced_at IS NULL;

-- 进行中的对局
SELECT id, p1_id, p2_id, expires_at, p1_choice IS NOT NULL AS p1_chose, p2_choice IS NOT NULL AS p2_chose
FROM rps_games WHERE status = 'choosing';

-- 某一局的全部账本记录（入场/下注、奖金、退款）
SELECT op_key, user_id, kind, delta_free, delta_paid, reason FROM coin_ledger
WHERE op_key LIKE 'gamble:12:%' OR op_key LIKE 'refund:gamble:12:%'
   OR op_key LIKE 'rps:12:%' OR op_key LIKE 'refund:rps:12:%' ORDER BY id;
```

一般不需要手工处理：恢复任务每 30 秒处理一次所有到期的局。某一局反复失败时日志里有
「结算轮次 N 失败」或「退款超时对局 N 失败」，先看数据库错误；不要直接改 `status`，
那会绕过余额变动，让账本与状态对不上。

### 变更检查

- 新增跨请求持有金币的游戏：状态进表，余额操作的 op_key 由表里的 id 派生，转换先锁行再判断状态，
  事务里不发消息，并在这里补充它的恢复策略。
- 测试：`tests/integration/test_gamble_rounds.py`、`tests/integration/test_rps_games.py`、
  `tests/integration/test_game_balances.py`（骰宝、御神签、RPG）、`tests/integration/test_game_state_schema.py`。
