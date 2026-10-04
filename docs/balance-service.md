# 余额服务契约

本页是金币变动的契约：所有改动用户余额或奖池的代码都按这里的规则写。实现在
`modules/core/balance.py`（用户余额）与 `modules/core/stake_reward_pool.py`（奖池），
账本表由迁移 `0019_coin_ledger` 创建。

## 规则

1. 金币只通过余额服务变动，不直接 `UPDATE user SET coins ...`。每次变动在 `coin_ledger` 留一行。
2. 每次变动带一个由**持久身份**派生的 `op_key`。同一个 `op_key` 重放不会重复生效。
3. 扣费必须检查结果。`debit` 余额不足时抛 `InsufficientBalance`，调用方不处理就不能继续后面的步骤，
   也不能向奖池贡献。
4. 扣费发生在交付之前，交付失败用 `refund` 退回；奖池贡献在交付成功之后。
5. 余额变动与调用方自己的业务状态写在同一个事务里。

## 调用示例

业务状态与余额一起提交（事务由调用方持有）：

```python
from core import balance, mysql_connection

async with mysql_connection.transaction() as connection:
    result = await balance.debit(
        connection,
        user_id,
        cost,
        op_key=balance.make_op_key("shop", purchase_id),
        reason="shop",
    )
    await connection.exec_driver_sql("INSERT INTO ...")  # 业务状态，与扣费同事务
```

余额不足与用户不存在都发生在任何写入之前，可以在事务内捕获后继续使用同一个事务；
任何其他异常都应让事务回滚。

扣费、交付、失败退款、成功后贡献奖池（没有其他状态要一起提交，用 `*_standalone`）：

```python
from core import balance, stake_reward_pool

op_key = balance.make_op_key("tl", chat_id, message_id)
try:
    await balance.debit_standalone(user_id, cost, op_key=op_key, reason="translate")
except (balance.InsufficientBalance, balance.UserNotFound):
    ...  # 提示余额不足，直接返回
    return

try:
    await deliver()
except Exception:
    await balance.refund_standalone(op_key, reason="translate_failed")
    ...
    return

await stake_reward_pool.credit_share_of_spend_standalone(cost, spend_op_key=op_key)
```

入账：

```python
await balance.credit(connection, user_id, 50, op_key=balance.make_op_key("topup", request_id),
                     reason="topup", kind=balance.CoinKind.PAID)
```

## API

| 符号 | 说明 |
|---|---|
| `credit(connection, user_id, amount, *, op_key, reason, kind=CoinKind.FREE, ref=None)` | 入账。`CoinKind.PAID` 同时把 `user_plan` 推导为 paid（管理员保持 admin） |
| `debit(connection, user_id, amount, *, op_key, reason, ref=None)` | 扣款，先扣免费再扣付费 |
| `refund(connection, original_op_key, *, reason="refund")` | 把一次成功的 `debit` 按原来的免费/付费拆分原路退回，只退一次 |
| `credit_standalone` / `debit_standalone` / `refund_standalone` | 同上，自己开事务，死锁时整个事务重跑 |
| `get_operation(op_key, *, connection=None, for_update=False)` | 查询已记录的操作，没有返回 `None` |
| `lock_user(connection, user_id)` | 锁住 user 行并返回当前余额，用于「先判断资格再变动」 |
| `run_in_transaction(work)` | 开事务执行 `work(connection)`，死锁时重跑；`work` 必须可安全重跑 |
| `make_op_key(*parts)` / `new_op_key(prefix)` / `refund_op_key(original)` | op_key 的构造与派生 |
| `audit_ledger()` | 对账，见下文 |
| `stake_reward_pool.credit_pool` / `debit_pool` / `credit_share_of_spend` 及 `*_standalone` | 奖池的同类操作 |

`amount` 必须是正整数；金额为 0 的消费由调用方自己跳过（例如免费翻译不调用 `debit`）。

### 结果与异常

返回 `BalanceResult`：`applied`、`op_key`、`kind`、`delta_free`、`delta_paid`、`balance_free`、
`balance_paid`，以及推导出的 `amount`、`balance_total`、`user_plan`。

- `applied=False` 表示这是重放：本次调用没有改动任何数据，其余字段是该操作最初生效时记录的值
  （`balance_*` 是当时变动后的余额，不是当前余额）。
- 扣款的免费/付费拆分取决于当时的余额，所以重放扣款只比较总额。

| 异常 | 含义 |
|---|---|
| `InsufficientBalance` | 余额不足，带 `balance_free` / `balance_paid` / `requested` |
| `UserNotFound` | 用户不存在 |
| `OperationConflict` | 同一个 `op_key` 已用不同的用户、金额（入账是免费/付费各自的金额）或类型记录过 |
| `RefundRejected` | 退款对象不存在，或不是 `debit` |
| `InvalidBalanceRequest` | 金额、`op_key`、`reason`、`ref` 不合法（同时是 `ValueError`） |
| `PoolInsufficient` | 扣减奖池时余额不足（`stake_reward_pool`） |

全部继承 `BalanceError`。`reason` 只是说明文字（至多 64 字符），不属于操作身份。

## op_key 命名

规则：

- 由持久身份派生：数据库 id、Telegram 的 chat id / message id / callback query id、日期。
  不用随机数、当前时间或进程内计数。
- 格式 `<领域>:<身份各部分用冒号连接>`，只含可打印 ASCII、无空白，至多 128 字符。
- `refund:` 前缀保留给 `refund`，服务会拒绝以它开头的 op_key；奖池贡献的 op_key 是 `pool:` 加消费的
  op_key，业务 op_key 同样不要以 `pool:` 开头。
- 没有持久身份时用 `new_op_key(prefix)`：每次调用都不同，因此没有重放保护，只作退路。

已使用的 op_key：

| 路径 | op_key | reason |
|---|---|---|
| 管理员人工充值 | `topup:<topup_requests.id>` | `topup` |
| 卡密兑换 | `redeem:<redemption_codes.id>` | `redeem_code` |
| 签到 | `checkin:<uid>:<日期>` | `checkin` |
| 抽奖 | `lottery:<uid>:<上一次抽奖时间 YYYYMMDDTHHMMSS，从未抽过为 never>` | `lottery` |
| AI 对话，每条消息一笔 | `chat:<chat_id>:<message_id>`，编辑后的消息 `…:edit:<edit_date 的 unix 秒>` | `ai_chat` |
| 翻译 `/tl` | `tl:<chat_id>:<message_id>` | `translate`，退款 `translate_failed` |
| 图片 `/pic` | `pic:<chat_id>:<message_id>` | `pic`，退款 `pic_failed` |
| 高清图 | `pic_hd:<callback query id>` | `pic_hd`，退款 `pic_hd_failed` |
| 奖池贡献 | `pool:<消费的 op_key>` | `spend_share` |

后续路径沿用同样的做法：游戏用持久化的轮次或对局 id（如 `rps:<game_id>:entry:<uid>`、
`gamble:<round_id>:bet:<uid>`），商店、质押、邀请等用各自的记录 id。

## 事务所有权

- 核心操作（`credit` / `debit` / `refund`）在**调用方的事务**里执行，不提交也不回滚。
  业务状态与余额一起提交或一起回滚。
- `*_standalone` 自己开事务，只用于没有其他状态要一起提交的场合。它们死锁时整个事务重跑，
  所以不要在事务外塞副作用。
- 事务里不要做外部副作用（发消息、调模型）。
- 锁顺序固定为先 `user` 行、后奖池行。需要自己的资格判断与余额变动串行时，先
  `lock_user`，再在锁内读取自己的状态。
- REPEATABLE READ 下，事务里更早的普通 `SELECT` 可能看不到已提交的并发写。
  需要最新值时用锁定读（`FOR UPDATE`），或保证事务的第一次一致性读发生在拿到 user 行锁之后。
  不要对可能不存在的唯一键做 `SELECT ... FOR UPDATE`：间隙锁会让两个首次写入互相死锁。
- 并发实现：先锁 user 行，同一用户的变动串行；op_key 判重以唯一约束为准——先 INSERT，
  撞上唯一键再用锁定读取回已有记录。细节见 `balance.py` 的模块 docstring。

## 失败、重放与退款

- **余额不足**：不改任何数据，不占用 `op_key`，补足余额后可以用同一个 `op_key` 重试。
  重放一次已经成功的扣款不检查当前余额。
- **重复投递**：同一个 Telegram update 被重复投递时 op_key 相同，`applied=False`。
  现有路径的做法是：这条已经收过钱，不再扣费、不再贡献奖池，业务照常继续——重复投递几乎只发生在
  进程中途被杀之后，此时用户付了钱却没有得到结果。
- **退款**：只针对已成功的 `debit`，原路退回，可重复调用，只退一次；退款的 op_key 是
  `refund:<原 op_key>`。没有扣过费就不要调用，调用也会被 `RefundRejected` 拒绝，不会凭空多出金币。
  退款本身可能失败，调用方要处理，并且不要在退款失败时告诉用户「已退还」。
- **奖池贡献**：和扣费同事务提交，或在交付成功后单独记账，op_key 为 `pool:<消费的 op_key>`；
  扣费失败、交付失败都不贡献。
- 同一个 op_key 配不同参数是调用方的 bug：抛 `OperationConflict`，不会改动数据。

## 账本与对账

`coin_ledger` 每次变动一行：`op_key`（唯一）、`user_id`、`kind`（`credit` / `debit` / `refund`）、
`delta_free`、`delta_paid`（带符号）、变动后的 `balance_free`、`balance_paid`、`reason`、
可选的 `ref`（退款指向原 op_key）、`created_at`。`(user_id, created_at)` 有索引。账本没有外键，
用户被删除后记录仍保留。

`stake_pool_ledger` 记录奖池变动：`op_key`（唯一）、`kind`、`delta`、`balance_after`、`reason`、
`ref`、`created_at`。奖池是 `DECIMAL(20,2)` 且不属于任何用户，所以与 `coin_ledger` 分开记账。

对账规则：

- 每个用户账本的末行余额等于 `user` 表的 `coins` / `coins_paid`。
- 相邻两行首尾相接：本行的「变动后余额 − 变动量」等于上一行的变动后余额。
- 第一行之前的余额（开户奖励等没经过账本的数量）由第一行隐含：变动后余额 − 变动量。
- 奖池账本末行的 `balance_after` 等于 `stake_reward_pool.balance`。

`balance.audit_ledger()` 检查第一、二、四条并返回 `DriftReport`（SQL 在 `balance.py` 的
`_BALANCE_MISMATCH_SQL`、`_BROKEN_CHAIN_SQL`、`_POOL_MISMATCH_SQL`）。它会扫描整张账本，
只在运维脚本里调用，不要放进请求路径。有偏差说明有代码绕过了余额服务直接改了余额。

## 旧接口的移除计划

`process_user` 里这些函数还在给尚未迁移的调用方使用，现在委托给余额服务：生成一次性 op_key、
`reason` 以 `legacy:` 开头，所以仍然写账本，但没有重放保护，也没有可对账的业务身份。
奖池的 `add_to_pool` / `subtract_from_pool` 同理（`stake_pool_ledger` 的 `legacy:` 记录）。

| 旧函数 | 替代 |
|---|---|
| `add_free_coins` | `balance.credit` |
| `add_paid_coins` | `balance.credit(..., kind=CoinKind.PAID)` |
| `spend_user_coins` | `balance.debit`，用异常区分余额不足与用户不存在 |
| `update_user_coins`、`async_update_user_coins` | `balance.credit` / `balance.debit` |
| `stake_reward_pool.add_to_pool` / `subtract_from_pool` | `credit_pool` / `debit_pool` |

调用方迁移完成后整组删除，不保留兼容层。查找剩余调用方：搜索这些函数名；
账本里 `reason LIKE 'legacy:%'` 的行显示它们在生产中还被谁触发。

不经过余额服务的变动：新用户注册时 `user` 行带着初始奖励一起插入（`features/profile/handlers.py`
的 `/me`，`features/economy/ref.py` 的邀请注册路径），这部分作为开户余额由账本第一行隐含。

## 测试

`tests/integration/` 里的余额、奖池、对话扣费、充值、签到抽奖测试使用真实 MySQL，夹具与运行方式见
[database-migrations.md](database-migrations.md) 的「运行集成测试」。造用户、读账本、Telegram 替身在
`tests/integration/economy_support.py`。纯逻辑（op_key 派生、拆分、套餐规则）的单元测试在
`tests/test_balance_unit.py`。
