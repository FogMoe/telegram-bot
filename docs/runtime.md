# 运行时：执行模型、准入、截止时间与关停

本页是对话主路径的执行模型契约：什么在事件循环里运行、什么在线程里运行，请求怎样被准入与限流，
整轮对话的截止时间覆盖什么，进程怎样停止，以及怎样观察这一切。权威定义是代码，本页讲规则并指向符号。
环境变量以 [`.env.example`](../.env.example) 为准。

## 执行模型

一条对话消息从进入到回复，所有步骤都在**同一个事件循环**里 `await`，没有「事件循环 → 线程 → 事件循环」的往返：

```
PTB handler（concurrent_updates 有界）
  └─ 每用户待处理数 → 会话锁 → 全局槽位          （handlers.py，准入，扣费之前）
       └─ ConversationTurn：plan → charge → … → model → … → delivery   （turn.py）
            └─ router.get_ai_response：provider 回退、熔断
                 └─ tool_runner.run_tool_loop
                      ├─ litellm.acompletion            原生 async
                      ├─ async 工具                      直接 await
                      ├─ 同步工具                        core.blocking.tools()  有界线程适配器
                      └─ TelegramVisibleContentHandler   直接 await bot 的发送
```

| 步骤 | 执行方式 |
|---|---|
| 模型调用（主聊天、摘要、回顾、翻译、识图、classifier、advisor） | `litellm.acompletion`，`create_chat_completion` 与 `run_ai_task` 都是 `async def` |
| 访问数据库的工具（记忆、日记、定时任务、摘要检索、善意赠币、印象） | `async def` 工具，直接 `await` 数据库调用 |
| 代用户执行 Telegram 命令的工具 | `async def`，在事件循环里 `await` 合成 update 的处理；命令在全新的 `contextvars` 上下文里运行（见下） |
| 可见内容与生成媒体的即时发送 | `TelegramVisibleContentHandler` 的 `__call__` / `send_tool_media` 是 `async def` |
| 只能同步的 SDK | 有界线程适配器，见「线程适配器清单」 |
| 每次模型调用前的 token 预算 | `core.blocking.compute()`（CPU 密集，不占事件循环） |
| 后台摘要 | `core.background` 登记的 asyncio 任务，同时最多 `SUMMARY_CONCURRENCY`（2）个 |

`core.db.run_sync` 不再出现在主路径上：它把协程投递回主事件循环并阻塞等待，只允许存在于明确的同步边界
（`process_user.*_sync` 兼容包装、`group_chat_history.get_group_context`，都没有调用者）。
`tests/test_runtime_boundaries.py` 用源码扫描守住这一点：`run_sync` 只在清单里的文件出现，
`run_coroutine_threadsafe` 只剩 `run_sync` 一处，`async def` 里不得直接调用 `time.sleep`、`requests.*`、
`UMFutures` 之类的阻塞调用，`ThreadPoolExecutor` 只在 `core/blocking.py`。

**代用户执行命令的上下文。** `execute_telegram_command` 在 `contextvars.Context()`（空上下文）里运行命令任务。
原因：调用它的这一轮对话带着「不记录 bot 自己发出的消息」的历史作用域，而命令的回复需要被记录和捕获，
这与命令由用户亲自发出时一致。旧实现经由 `run_coroutine_threadsafe` 恰好得到了空上下文，这里显式保持。
`tests/test_telegram_command_tool.py::test_delegated_command_runs_in_a_clean_history_context` 钉住它。

## 准入

准入在 `features/conversation/handlers.py`，状态在 `core/admission.py`。所有拒绝都发生在**一轮对话开始之前**，
而扣费是一轮对话的第二个阶段（`charge`），所以被拒绝的请求一定没有扣费。

### 顺序与会话锁的关系

对一个 AI 对话请求（私聊总是；群聊要回复 bot 或带触发词），按下面的顺序：

1. **每用户待处理数**（`user_pending`）：同一个会话（用户）同时「正在处理 + 等待会话锁」的轮次不超过
   `CHAT_MAX_PENDING_PER_USER`，超过立即拒绝。
2. **会话锁**（`conversation_locks`）：同一个用户一次只运行一轮。等锁的时间算排队，受整轮截止时间约束。
3. **全局槽位**（`AdmissionController.slot`）：同时运行的轮次不超过 `CHAT_MAX_CONCURRENT_TURNS`。
   拿不到槽位的在 FIFO 队列里等待，队列最长 `CHAT_MAX_QUEUED_TURNS`，最多等
   `CHAT_QUEUE_MAX_WAIT_SECONDS`（也不会超过自己的整轮截止时间）。
4. 拿到槽位之后才进入 `run_turn`，槽位在整轮结束（含投递）后释放。

会话锁和准入是两件事：**会话锁保证同一用户的轮次串行（正确性），准入限制容量（保护进程与上游）**。
由于有会话锁，同一用户永远不会同时占用两个全局槽位，所以「每用户上限」限制的不是并发，而是排在锁后面的深度。

群聊里不唤起 AI 的消息（绝大多数）不计入每用户待处理数，也不占全局槽位，不会收到「繁忙」提示；
它们仍然照常记进群聊上下文。被拒绝的群聊请求同样会先记进群聊上下文。
后台任务（定时任务、空闲跟进、摘要）不经过全局槽位，它们各自有自限：定时任务一次只处理一个，
空闲跟进一批最多 `IDLE_FOLLOWUP_BATCH_SIZE`（3）个，摘要最多 2 个，同步工具共用线程适配器的上限。

### 配置与取值依据

| 配置 | 默认 | 含义与依据 |
|---|---|---|
| `CHAT_MAX_CONCURRENT_TURNS` | 32 | 同时运行的轮次。模型调用是原生 async，不再受线程数限制；真正的约束是上游 provider 的速率限制与 MySQL 连接池（默认 5 + 溢出 10，每个阶段是短事务，不跨模型调用持有连接）。32 是单进程小中型部署下不触发上游限速的保守值 |
| `CHAT_MAX_QUEUED_TURNS` | 32 | 等槽位的队列长度。有界队列让过载时「立即告诉用户」而不是无限堆积；它也限制了「被挂起的对话 handler」数量，见 `TELEGRAM_CONCURRENT_UPDATES` |
| `CHAT_MAX_PENDING_PER_USER` | 3 | 一个在跑、一个在等锁、再多一条余量。私聊里连发的消息已经被批处理窗口合并，所以真实用户很少触及它；它挡的是刷屏 |
| `CHAT_QUEUE_MAX_WAIT_SECONDS` | 20 | 超过这个等待就告诉用户繁忙。用户对「立刻被告知」的容忍度远高于「干等很久才被拒」；0 表示不排队，满了就拒绝 |
| `CHAT_TURN_DEADLINE_SECONDS` | 360 | 整轮截止时间，见下一节 |
| `TELEGRAM_CONCURRENT_UPDATES` | 128 | PTB 同时处理的 update 数，替代原来的 `concurrent_updates(True)`（256） |
| `BLOCKING_TOOL_THREADS` | 8 | 同步工具线程池大小 |
| `BLOCKING_IO_THREADS` | 4 | 事件循环回调里零星同步网络调用的线程池大小 |
| `RUNTIME_METRICS_LOG_INTERVAL_SECONDS` | 300 | 指标汇总日志间隔，0 关闭 |
| `RUNTIME_SHUTDOWN_GRACE_SECONDS` | 8 | 收到停止信号后给在途轮次的宽限；要小于进程管理器的停止超时（Compose 的 `stop_grace_period` 已设为 30 秒，`runBot.sh` 的 `BOT_STOP_TIMEOUT` 默认 15 秒） |

**`TELEGRAM_CONCURRENT_UPDATES` 的取值依据。** 这个值限制「同时在执行的 handler」。被准入挂起的对话 handler 也占着名额
（等槽位、等会话锁、等批处理窗口），如果名额被它们占满，`/lottery`、`/me` 这类与 AI 无关的命令也会排队。
挂起的对话 handler 数量上限约为 `CHAT_MAX_CONCURRENT_TURNS + CHAT_MAX_QUEUED_TURNS`（64），再加上批处理窗口里的合并消息，
所以默认 128 给命令与回调留下约一半的余量，又明显小于原来的 256。调高 `CHAT_MAX_CONCURRENT_TURNS` 时应当同步调高它；
`tests/test_runtime_lifecycle.py` 检查默认值满足「不小于槽位 + 队列」。

**同步工具线程池大小。** 8 个线程 × 同步工具的典型耗时（HTTP 工具 1–10 秒，生图最长 `IMAGE_GEN_TIMEOUT`，沙箱最长 60 秒）
给出同步工具的吞吐上限。原来 10 个线程要同时承担模型调用、工具和跨线程投递，现在模型调用不占线程，
8 个线程只用于工具。工具排队的深度与等待在 `blocking.queued` / `blocking.wait_seconds` 里可见。

### 过载时用户看到什么

| 情形 | 提示（`handlers.py`） | 指标 `admission.rejected{reason=...}` |
|---|---|---|
| 队列已满 | `BUSY_TEXT`：雾萌娘现在有点忙，请稍后再试，**这条消息没有扣除硬币** | `queue_full` |
| 等槽位超过阈值 | `BUSY_TEXT` | `queue_timeout` |
| 排队时整轮截止时间已到（含等会话锁） | `BUSY_TEXT` | `deadline` |
| 同一用户待处理的轮次太多 | `USER_BUSY_TEXT`：前面还有消息在处理，等回复之后再发 | `user_limit` |
| 进程正在停止 | `SHUTTING_DOWN_TEXT`：正在重启 | `shutting_down` |

提示直接回复在用户的那条消息上；发送失败只记 debug 日志，不影响处理。被拒绝的消息没有进入对话历史。

## 整轮截止时间

从请求进入队列（拿会话锁之前）开始计时，时长 `CHAT_TURN_DEADLINE_SECONDS`。`core/deadline.py` 的 `Deadline.guard()`
是协作式取消点：到期时取消正在等待的那个 `await`（模型 HTTP 请求、async 工具、Telegram 发送都是真正的任务取消），
然后由调用方收尾。

| 到期时所处的位置 | 行为 |
|---|---|
| 排队（等会话锁、等全局槽位） | 拒绝，`BUSY_TEXT`，**没有扣费** |
| `prepare`（下载并识别媒体） | 回复超时提示，状态 `TurnStatus.DEADLINE_EXCEEDED`，这一轮已扣费，不退 |
| `model`：等待模型响应 | 取消请求，不再换下一个 provider，回复 `TURN_DEADLINE_ERROR_MESSAGE`；这个 provider 计一次熔断失败 |
| `model`：运行工具 | 取消工具（线程里的同步工具只是不再等它）；回复 `TURN_DEADLINE_ERROR_MESSAGE` |
| `model`：即时发送可见内容或媒体 | 取消发送，同上 |
| `delivery`（最终回复与媒体） | 受截止时间加 30 秒宽限约束，宽限让超时提示本身还能发出去；到期后放弃剩余投递，`finalize` 照常执行 |

要点：

- **扣费规则不变。** 扣费发生在 `charge` 阶段，截止时间不触发退款，也没有补偿式回滚；扣费、历史写入的顺序与语义都与改造前一致。
  是否对「扣费之后超时」的轮次退款是产品决定，本次没有实现，见文末「建议」。
- **历史保持配对。** 工具阶段到期时，`tool_runner` 给这一轮里每个还没有结果的 `tool_call` 补一条结果：
  正在运行的那个是 `{"error": "interrupted", "outcome": "unknown", ...}`（可能已经完成，不要假定失败、不要重复执行），
  后面没来得及运行的是 `{"error": "not_executed", ...}`。这样 assistant 的 `tool_calls` 与 `tool` 结果一一配对，
  下一轮不会因为不配对被 provider 拒绝。超时提示是固定的运行时错误文案（`router.runtime_error_cause` 识别为
  `turn_deadline_exceeded`），不当作 AI 回复写进历史，按错误通知的作用域投递。
- **不重复执行工具副作用。** 到期之后不会换 provider 重跑，也不会重试已经开始的工具；已经在线程里运行的同步工具无法被中断，
  线程自己跑完、结果被丢弃（线程适配器保证：还在排队、没开始的调用被取消后不会再执行）。
- **单次模型调用的超时被收紧。** `AI_CHAT_COMPLETION_TIMEOUT_SECONDS`（默认 300 秒）会被收紧到剩余的截止时间之内（至少 1 秒），
  所以一次卡死的 provider 调用不会吃掉比整轮预算更多的时间。默认 360 秒的整轮预算略大于单次调用上限；
  如果希望一个 provider 超时之后还有时间回退到下一个，把 `AI_CHAT_COMPLETION_TIMEOUT_SECONDS` 调到明显小于整轮预算。
- **停止时的「提前到期」。** 进程停止时 `Deadline.expire("shutdown")` 让在途轮次的截止时间提前到期，用户收到
  `TURN_SHUTDOWN_ERROR_MESSAGE`（正在重启，请重新发送），不计为 provider 失败。
- **与 E 的租约契约。** 定时任务与空闲跟进不使用整轮截止时间，沿用 `job_claims.run_leased` 的租约、续期与执行上限；
  `abort_event`（`threading.Event`）仍然在每次模型调用前、每个工具执行前、每次可见内容发送前被检查，租约丢失仍然阻止后续投递。
  不同的是：工具循环现在跑在事件循环里，租约丢失时 `run_leased` 对 worker 的**取消**会直接取消正在等待的模型调用与 async 工具，
  不再要等到下一个检查点；`abort_event` 继续覆盖线程里的同步工具与发送前的检查。详见 [job-recovery.md](job-recovery.md)。

## 线程适配器清单

线程只出现在 `core/blocking.py` 的三个有界适配器里（`ThreadPoolExecutor` 不得出现在别处）。适配器的规则：

- 并发有上限；超出的排队，排队深度与等待时间进指标；
- 等待方被取消时，还没开始的调用不会再执行，已经在跑的无法中断（线程跑完、结果丢弃）；
- 调用时复制 `contextvars`，线程里的工具能读到本次请求的 `tool_request_context`；
- `shutdown()` 取消排队中的调用并释放线程池，关停后新调用抛 `AdapterClosedError`。

| 适配器 | 大小 | 用途 |
|---|---|---|
| `blocking.tools()` | `BLOCKING_TOOL_THREADS` | 同步 AI 工具（下表） |
| `blocking.io()` | `BLOCKING_IO_THREADS` | 事件循环回调里的零星同步网络调用：`crypto/monitoring.py` 的价格检查、`crypto_predict.get_btc_price`、`sticker_sender` 的贴纸元数据查询 |
| `blocking.compute()` | `min(4, CPU 数)` | `litellm_client` 每次模型调用前的 token 预算与消息整理（CPU 密集） |

**仍然同步的工具（留在 `tools()` 里）与理由：**

| 工具 | 实现 | 为什么不改成 async |
|---|---|---|
| `google_search`、`fetch_url` | `requests` + `trafilatura` | 错误脱敏路径（URL 里的凭据、`requests` 异常类型）有基于 `requests` 的夹具测试，换 aiohttp 会改变异常形态，风险大于收益；`trafilatura` 抽取本身是同步 CPU 工作 |
| `execute_python_code`（Judge0） | `requests` | 同上，单次 POST |
| `generate_image`、`generate_voice` | `requests`，响应体较大，最长 `IMAGE_GEN_TIMEOUT` | 二进制响应与多步校验逻辑与 `requests.Response` 耦合，改写面大 |
| `linux_sandbox`（E2B） | e2b 同步 SDK，沙箱句柄保存在请求上下文里，结束时 `kill()` 也是阻塞调用 | 沙箱生命周期绑定在请求上下文上；清理走 `cleanup_linux_sandbox_async`（适配器线程） |
| `list_available_stickers` | `urllib` | 本地缓存加偶发的 Telegram 请求 |
| `get_help_text`、`read_doc` | 纯内存 | 标记为内联（`@inline_tool`），直接在事件循环里调用，不进线程 |

这些工具持有的 `requests.Session`（每线程一个）通过 `core/http_sessions.py` 登记，停止时统一关闭。

**不在适配器里、也不在本次范围内的遗留：**

- `features/economy/web_password.py` 的 `asyncio.to_thread(hash_password, …)`：Argon2id 哈希，纯 CPU、不碰网络，用事件循环的默认线程池，属于 economy 模块。
- `features/games/rpg/utils.py` 的 `rpg_db_executor`：没有任何调用者的遗留对象，属于 games 模块。
- `core/chat_records.py` 等处同步的 token 估算（CPU）仍在事件循环里（每次十几毫秒量级）。

`tests/test_runtime_boundaries.py` 的清单（`ALLOWED_*`）记录了上述例外，新增例外必须在那里写明理由。

**新增工具时怎么选：** 访问数据库或调用别的 async 能力的，写成 `async def`；只能同步的 SDK，写成普通函数（自动进 `tools()`）；
只有纯内存、微秒级、不做任何 I/O 的才允许 `@inline_tool`。不要在 `async def` 里直接调用 `requests`、`time.sleep` 或同步 SDK。

## 取消与关停

PTB 的停止流程是：停止拉取 update → 等在途的 handler 与 job 结束 → `post_stop`。`BotApplication.stop`（`app/bot_app.py`）
在 PTB 开始停止的**第一刻**调用 `runtime_lifecycle.begin_shutdown()`，否则排队中的对话会白白等到自己的截止时间：

1. 准入关闭：新请求与排队中的请求立刻被拒绝（`SHUTTING_DOWN_TEXT`，还没扣费）；
2. 在途的轮次获得 `RUNTIME_SHUTDOWN_GRACE_SECONDS` 的宽限，之后它们的截止时间提前到期，用户收到「正在重启」的提示。

然后 PTB 等在途 handler 结束，进入 `post_stop`，`runtime_lifecycle.shutdown_runtime()` 按下面的顺序释放资源，
**每一步独立 try，失败只记录，不影响后面的步骤**：

| 顺序 | 步骤 | 做什么 |
|---|---|---|
| 1 | 停止接收新工作 | 准入关闭（幂等），取消宽限计时器 |
| 2 | 取消并等待挂起任务 | `core.background` 登记的任务：后台摘要、指标汇总、行情监控与延迟结果检查；批处理窗口在 handler 里，PTB 已经等过了 |
| 3 | 刷新历史 | `flush_all_pending_events()`：把还没写完的 Telegram 历史事件写进数据库（此时数据库还开着） |
| 4 | 关闭 HTTP 客户端 | LiteLLM 缓存的异步 HTTP 客户端（`litellm_client.close_clients`）；同步工具登记的 `requests` 会话（`http_sessions`）。Telegram 自己的连接由 PTB 的 `shutdown()` 在 `post_stop` 之后关闭 |
| 5 | 关闭线程适配器 | 取消排队中的同步调用，释放线程；已经在跑的线程不等待 |
| 6 | 关闭数据库连接池 | `db.dispose_engine()`，之后再使用会按当时的配置重新创建引擎 |

**E 的「关停释放未开始的 claim」语义不变。** 定时任务与空闲跟进在拿到会话锁之后检查 `Application.running`，
已经停止就把还停在 `claimed` 阶段的 claim 释放（`released`，不计为一次尝试）；`worker` 被取消时同样释放。
PTB 的 `stop()` 会等这两个 job 结束，所以走到 `post_stop` 时释放已经完成。已经进入 `generating` / `delivering` 的任务不强求优雅完成，
强制终止时 claim 留在库里，租约到期后按 [job-recovery.md](job-recovery.md) 的策略回收。

## 指标

`core/metrics.py` 是进程内的轻量指标（计数器、仪表、固定桶的直方图），不依赖外部系统。
`MetricsReporter` 每 `RUNTIME_METRICS_LOG_INTERVAL_SECONDS` 秒写一行 INFO 日志，停止时再写最后一行。
计数器与直方图是**这一段时间的变化**，仪表是写日志那一刻的当前值，没有任何变化时写 `idle`：

```
runtime metrics (last 300s): admission.admitted=118 admission.rejected{reason=queue_timeout}=2 provider.calls{provider=gemini}=240 provider.failures{provider=gemini}=3 tool.calls{tool=google_search}=41 turn.finished{status=completed}=117 admission.queue_depth[n=120 p50=0.000 p95=3.000 max=9.000] admission.queue_seconds[n=118 p50=0.000 p95=0.412 max=18.350] provider.call_seconds{provider=gemini}[n=240 p50=2.110 p95=9.800 max=41.200] turn.run_seconds[n=117 p50=5.300 p95=28.000 max=80.100] admission.queued=2 admission.running=14
```

查看方式：`grep "runtime metrics" logs/tgbot.log`（Docker：`docker compose logs bot | grep "runtime metrics"`）。
分位数由桶估算（上限为这一段时间观察到的最大值），不保留每一个样本。

| 要回答的问题 | 指标 |
|---|---|
| 排队深度 | 仪表 `admission.queued`（当前）、`admission.running`；直方图 `admission.queue_depth`（每个请求到达时看到的深度，单位是个数）；同步工具线程池的 `blocking.queued{pool}` |
| 排队延迟 | `admission.queue_seconds`（等全局槽位）、`turn.queue_seconds`（等会话锁 + 等槽位，也是 `TurnTimings.queue_seconds`）、`blocking.wait_seconds{pool}` |
| 整轮耗时 | `turn.run_seconds`（不含排队）、`turn.total_seconds`（含排队）、`provider.call_seconds{provider}`、`tool.seconds{tool}`、`blocking.run_seconds{pool}` |
| 超时次数 | `turn.deadline_hits{reason=deadline\|shutdown, phase=prepare\|model\|tool\|delivery}`、`provider.timeouts{provider}`（单次调用超时） |
| 过载 | `admission.rejected{reason=...}`、`admission.admitted` |
| provider 失败 | `provider.failures{provider}`、`provider.calls{provider}` |
| 工具失败 | `tool.failures{tool}`（执行异常，或返回了 `error`）、`tool.calls{tool}` |
| 一轮的结局 | `turn.finished{status=...}`（`TurnStatus` 的取值） |

标签只用有限集合：provider 名、注册过的工具名（模型编造的工具名记为 `unknown`）、拒绝原因、阶段。**不要把用户 id 放进标签。**

## 基准

`scripts/bench_runtime.py` 用 fake provider、fake 数据库与 fake Telegram（不连网）驱动 N 个并发对话走完整的
`handlers._reply_locked` 入口：一次带可见文本与工具调用的模型回复 → 一个同步阻塞的工具 → 最终回复。
同一份脚本可以指向不同版本的 `modules/` 目录，用来比较改造前后：

```bash
python scripts/bench_runtime.py --label after
python scripts/bench_runtime.py --modules <改造前的 modules 目录> --label before
# 放宽准入，测原生 async 本身的吞吐：
python scripts/bench_runtime.py --max-concurrent 256 --max-queued 1024 --queue-wait 60
```

**这是本地合成负载，不是生产测量。** 假设：每次模型调用 300 ms（两次），同步工具阻塞 50 ms，Telegram 每次调用 20 ms，
数据库每次调用 5 ms，每个对话来自不同用户；各并发度重复 3 次取中位数。单位秒（除标明的以外）。
「改造前」是合并本工作流之前的集成分支（10 线程池 + `run_in_executor` + 跨线程投递，`concurrent_updates` 无准入）。

| 配置 | 并发对话 | 跑完 | 拒绝（繁忙） | 墙钟 | 整轮 p50 | 整轮 p95 | 模型阶段 p95 | 排队 p95 | 吞吐（轮/秒） |
|---|---|---|---|---|---|---|---|---|---|
| 改造前 | 10 | 10 | – | 0.97 | 0.97 | 0.97 | 0.83 | 0 | 10.3 |
| 改造前 | 50 | 50 | – | 4.10 | 2.52 | 4.07 | 3.93 | 0 | 12.2 |
| 改造前 | 200 | 200 | – | 15.77 | 8.33 | 14.99 | 14.85 | 0 | 12.7 |
| 改造后，默认限制 | 10 | 10 | 0 | 1.03 | 0.99 | 1.03 | 0.89 | 0 | 9.7 |
| 改造后，默认限制 | 50 | 50 | 0 | 2.06 | 1.12 | 2.02 | 1.00 | 1.05 | 24.3 |
| 改造后，默认限制 | 200 | 64 | 136 | 2.11 | 1.54 | 2.11 | 1.00 | 1.11 | 30.3 |
| 改造后，放宽限制 | 10 | 10 | 0 | 1.02 | 0.98 | 1.02 | 0.88 | 0 | 9.8 |
| 改造后，放宽限制 | 50 | 50 | 0 | 1.29 | 1.15 | 1.25 | 1.11 | 0 | 38.9 |
| 改造后，放宽限制 | 200 | 200 | 0 | 2.20 | 1.55 | 2.15 | 2.01 | 0 | 90.7 |

怎么读：

- 低并发（10）时两者一样，延迟由 provider 与工具决定（约 1 秒）。
- 并发超过 10 之后，改造前的 10 线程池成为瓶颈：吞吐固定在约 12 轮/秒，200 个并发对话的 p95 达到 15 秒，而且**没有显式排队**，
  等线程的时间混在「模型阶段」里，用户看不到也无从限流。
- 改造后模型调用不再占线程。放宽限制时 200 个并发对话的 p95 为 2.2 秒，吞吐约 91 轮/秒（约 7 倍）；
  此时的瓶颈变成同步工具的 8 个线程（200 × 50 ms ÷ 8 ≈ 1.25 秒），这正是有界适配器应有的行为。
- 默认限制（32 并发 + 32 排队）下，200 个并发对话里 64 个被接受，**136 个在 2 秒内被明确告知繁忙（不扣费）**，
  被接受的对话 p95 为 2.1 秒。这是设计内的过载行为：容量由配置决定，不是靠无限排队吸收。
- 事件循环的最大调度延迟在两种模型下都是 15 ms 量级（Windows 的 15.6 ms 系统时钟粒度），这个场景里区分不出来，没有据此下结论。

局限：provider、数据库、Telegram 的延迟是固定值，没有尾延迟、限速与重试；同步工具是 `time.sleep`，不代表真实的 `requests`；
不包含模型响应体的解析与 JSON 序列化的 CPU 开销；吞吐数字依赖运行机器。需要真实容量结论时，应当用生产的指标行（上一节）测量。

## 变更检查

- 新增会调用模型的路径：用 `await create_chat_completion` / `await run_ai_task`，不要自己 `litellm.completion`；
  需要整轮约束的把 `Deadline` 传下去（`get_ai_response(deadline=...)`）。
- 新增后台任务：用 `core.background.spawn`，不要裸 `asyncio.create_task`，这样关停时会被取消和等待。
- 新增需要同步 SDK 的地方：放进对应的适配器，并更新上面的清单与 `tests/test_runtime_boundaries.py`。
- 新增准入拒绝的原因：在 `OverloadReason` 里加，给出用户提示，并在「过载时用户看到什么」里补一行。
- 测试：`tests/test_admission.py`、`tests/test_conversation_admission.py`、`tests/test_deadline.py`、`tests/test_blocking.py`、
  `tests/test_async_execution.py`、`tests/test_runtime_lifecycle.py`、`tests/test_runtime_boundaries.py`、`tests/test_metrics.py`。

## 建议（未实现）

- **扣费之后超时是否退款。** 当前规则是不退（与媒体识别失败一致）。如果产品希望退，需要在 `charge` 的 `op_key` 体系下增加按轮次的退款操作
  （`balance.refund` 引用本轮的 `op_key`），并区分「模型阶段还没有任何可见输出就超时」与「已经发出部分内容再超时」；
  改动面在 `conversation/billing.py` 与 `turn.py`，需要单独确认规则。
- **后台任务的容量。** 定时任务、空闲跟进与摘要目前各自限流、不经过全局槽位。如果它们在高峰时与用户对话争用 provider 额度，
  再考虑给它们一个低优先级的共享槽位。先用 `provider.calls` 与 `turn.run_seconds` 的指标确认是否真的有争用。
