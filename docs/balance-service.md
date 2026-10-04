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
| `lock_users(connection, user_ids)` | 按 user id 升序锁住多个 user 行，一个事务变动多个用户（转账、邀请奖励）时使用 |
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
- 以命令消息为身份的 op_key 用 `core/command_identity.message_identity(chat_id, message_id)` 拼接。
  AI 代用户执行的命令复用触发对话的那条消息 ID，同一轮里可能有好几条；代执行时这一段变成
  `<chat_id>:<message_id>:ai:<代执行身份>`（由那一轮的消息、编辑版本与命令文本派生），所以下表里
  `<chat_id>:<message_id>` 形式的 op_key 在代执行时都会多出 `:ai:<…>`。同一身份被参数不同的操作再次使用时
  不是重放：`/give` 返回 `CONFLICT`，其余走 `balance` 的 `OperationConflict`。

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
| 新用户开户奖励（`/me`、邀请注册） | `signup:<uid>` | `signup` |
| 邀请奖励：被邀请人 / 邀请人 | `ref_invitee:<被邀请人 uid>` / `ref_referrer:<被邀请人 uid>` | `ref_invitee` / `ref_referrer` |
| 任务奖励 | `task:<uid>:<task_id>` | `task` |
| 商店：记忆上限 / 权限升级 | `shop:memory:<callback query id>` / `shop:perm<等级>:<callback query id>` | `shop_memory` / `shop_permission` |
| 商店：刮刮乐、欢乐彩 | 扣款 `shop:<scratch\|huanle>:<callback query id>`，开奖 `…:win`，保底 `…:bonus` | `shop_<item>` / `shop_<item>_win` / `shop_<item>_bonus` |
| 质押 | `stake:<chat_id>:<message_id>` | `stake` |
| 质押领奖（用户入账与奖池扣减共用同一个 op_key，分属两张账本） | `stake_collect:<uid>:<stake_time>:<领奖窗口起点>` | `stake_reward` |
| 质押赎回：本金 / 顺带结算的回报（回报同样两张账本共用） | `stake_withdraw:<uid>:<stake_time>` / `stake_withdraw_reward:<uid>:<stake_time>` | `stake_withdraw` / `stake_reward` |
| `/give`：发送者扣款（本金加手续费）/ 收款人入账 | `give:<chat_id>:<message_id>` / `give:<chat_id>:<message_id>:recv` | `give` / `give_received` |
| `/bribe` | `bribe:<chat_id>:<message_id>` | `bribe` |
| BTC 预测：下注 / 中奖 / 过期退款 | `btc:<uid>:<开始时间>:bet` / `:win` / `:expired`（见下） | `btc_bet` / `btc_win` / `btc_expired`，退款 op_key 为 `refund:btc:<uid>:<开始时间>:bet` |
| `/swap` 兑换 | `swap:<chat_id>:<message_id>` | `swap` |
| AI 善意赠币 | `kindness:<收款人 uid>:<上一次赠币时间，从未赠过为 never>` | `kindness` |
| 多人下注，每人每轮一笔 | `gamble:<gamble_rounds.id>:bet:<uid>`，同时记在 `gamble_bets.op_key` | `gamble_bet`，中奖者账户不存在而改为全额退款时 `gamble_refund` |
| 多人下注的奖金 | `gamble:<round_id>:payout` | `gamble_win` |
| 石头剪刀布入场费 | `rps:<rps_games.id>:entry:<uid>` | `rps_entry`，退款 `rps_draw`（平局）、`rps_timeout`（超时）、`rps_failed`（创建失败） |
| 石头剪刀布奖金 | `rps:<game_id>:win` | `rps_win` |
| 骰宝，每个面板消息一局 | `sicbo:<chat_id>:<message_id>:bet` 与 `sicbo:<chat_id>:<message_id>:win` | `sicbo_bet`、`sicbo_win` |
| 御神签，每人每天一次 | `omikuji:<uid>:<日期>` | `omikuji` |
| RPG 回血 | `rpg:heal:<chat_id>:<message_id>`（命令消息） | `rpg_heal` |
| RPG 击败怪物 | `rpg:monster:<chat_id>:<message_id>:reward` | `rpg_monster` |
| RPG 玩家对战 | `rpg:pvp:<chat_id>:<message_id>:loss`（败者）与 `rpg:pvp:<chat_id>:<message_id>:win`（胜者） | `rpg_pvp_loss`、`rpg_pvp_win` |

时间戳一律是 `YYYYMMDDTHHMMSS`。几条身份的来由：

- 质押记录以 `user_id` 为主键、没有自己的 id，所以用 `stake_time`（以及领奖窗口起点 `last_reward_time`，
  从未领过为 `stake_time`）标识「哪一次质押的哪一段窗口」。领奖成功后窗口起点前进，同一窗口不会再领。
- 预测记录同样以 `user_id` 为主键，用开始时间标识；新预测只能在上一条结算或过期处理之后创建，
  开始时间不会重复。创建时去掉微秒，因为 MySQL 的 DATETIME 会对微秒四舍五入，op_key 必须与读回的值一致。
  升级到账本之前创建、没有 `bet` 记录的预测，过期时退不了款，改用 `:expired` 入账原额。
- 抽奖、善意赠币的资格窗口只随着时间戳写入而前进，所以 op_key 由「上一次的时间戳」派生（与 `lottery:` 同理）。
- 商店以按钮回调的 query id 为身份：同一次点击被重复投递不会重复扣款与发放，两次不同的点击是两次购买。

游戏用持久化的轮次或对局 id（如 `rps:<game_id>:entry:<uid>`、`gamble:<round_id>:bet:<uid>`）。游戏状态的持久化与重启恢复见
[job-recovery.md](job-recovery.md) 的「游戏状态」。

## 事务所有权

- 核心操作（`credit` / `debit` / `refund`）在**调用方的事务**里执行，不提交也不回滚。
  业务状态与余额一起提交或一起回滚。
- `*_standalone` 自己开事务，只用于没有其他状态要一起提交的场合。它们死锁时整个事务重跑，
  所以不要在事务外塞副作用。
- 事务里不要做外部副作用（发消息、调模型）。
- 锁顺序固定为先 `user` 行、后奖池行。需要自己的资格判断与余额变动串行时，先
  `lock_user`，再在锁内读取自己的状态。领奖这类要在锁内按奖池余额决定发多少的操作，
  先 `lock_user`，再 `get_pool_balance(for_update=True)`，不能反过来：反过来会和
  「扣费后贡献奖池」的路径互相等待。
- 一个事务涉及多个用户（`/give` 的发送者与收款人、邀请的邀请人与被邀请人）时，用 `lock_users`
  按 user id 升序一次锁完，再做任何写入。A 赠 B 与 B 赠 A 同时发生也不会死锁。
  需要依据名字等信息先解析出对方 id 的，在事务之前解析。
- 同一用户的业务状态（每日次数、质押记录、待处理请求）在 `lock_user` 之后用普通读取即可，
  不要对可能不存在的键 `FOR UPDATE`。
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

## 余额入口清单

每个改动用户余额或奖池的入口，以及它的事务边界。「同事务」指余额变动与该行写的业务状态在一个事务里提交，
任何一步失败整体回滚，所以失败不需要退款；只有「先扣费、再做事务之外的交付」的路径才有 `refund`。
`debit` 余额不足都发生在写入之前，调用方据此直接返回，不继续后面的步骤，也不贡献奖池。

### 商店、质押、转账、奖励（`features/economy/`、`features/crypto/`、`features/ai/tools/`、`features/profile/`）

| 入口 | 变动 | op_key | 事务边界 |
|---|---|---|---|
| `/me`（`profile/handlers.py` `me`） | 开户奖励入账 | `signup:<uid>` | 注册 `INSERT` 与入账同事务；是否首次开户在事务外判断，并发的两次首次 `/me` 由 op_key 保证只入账一次，老用户不会补发 |
| 邀请注册 / `/ref`（`economy/operations/invitations.py` `add_invitation_record`） | 开户奖励（新用户）、被邀请人与邀请人各一笔邀请奖励 | `signup:<uid>`、`ref_invitee:<uid>`、`ref_referrer:<uid>` | 开户、`user_invitations` 记录与三笔入账同事务；双方按 id 升序锁定；`user_invitations` 以被邀请人为主键，同一被邀请人只奖励一次，重复邀请在任何入账之前被唯一键拒绝 |
| `/checkin`（`economy/operations/checkin.py` `process_checkin`） | 签到奖励入账 | `checkin:<uid>:<日期>` | 锁用户行 → 读签到记录 → 写签到日期 → 入账，同事务；今天已签到的不入账 |
| `/lottery`（`economy/operations/lottery.py` `lottery`） | 抽奖奖励入账 | `lottery:<uid>:<上一次抽奖时间>` | 锁用户行 → 读时间戳（24 小时冷却）→ 入账 → 写时间戳，同事务 |
| `/charge` 卡密（`economy/operations/charge.py` `redeem_code`） | 付费金币入账 | `redeem:<redemption_codes.id>` | 锁卡密行 → 入账 → 标记已使用，同事务；进程内另有「处理中」标记挡住同一进程的并发请求 |
| 管理员充值（`economy/operations/charge.py` `decide_topup_request`） | 付费金币入账 | `topup:<topup_requests.id>` | 占住 `pending` 的转换（影响行数为 1）→ 入账，同事务；入账失败状态保持 pending |
| `/task`（`economy/operations/task.py`） | 任务奖励入账 | `task:<uid>:<task_id>` | 锁用户行后确认未完成，入账与 `user_task` 记录同事务；用户不存在时不写完成记录 |
| 商店（`economy/operations/shop.py`）购买记忆上限、权限升级 | 扣款 | `shop:memory:<qid>`、`shop:perm<等级>:<qid>` | 扣款与 `permanent_records_limit` / `permission` 更新同事务 |
| 商店刮刮乐、欢乐彩 | 扣款、开奖入账、保底奖励 | `shop:<item>:<qid>`、`…:win`、`…:bonus` | 三者同事务；进程内的保底计数只在提交后更新，回滚不会留下半个计数；重复点击只读回第一次的结果 |
| `/stake`（`economy/operations/stake.py` `open_stake`） | 扣款 | `stake:<chat>:<msg>` | 扣款与 `user_stakes` 记录同事务 |
| 质押领奖（`collect_stake_reward`） | 用户入账、奖池扣减 | `stake_collect:…`（两张账本共用） | 入账、奖池扣减、`last_reward_time` 推进同事务；先 `lock_user` 再锁奖池行，按锁内余额决定发放的周期数 |
| 质押赎回（`withdraw_stake_principal`） | 本金入账、回报入账与奖池扣减 | `stake_withdraw:…`、`stake_withdraw_reward:…` | 入账、奖池扣减、删除质押记录同事务 |
| `/give`（`economy/operations/coins.py` `transfer_coins`） | 发送者扣款（本金加手续费）、收款人入账 | `give:<chat>:<msg>`、`…:recv` | 扣款、入账、`user_give_daily` 次数同事务；双方按 id 升序锁定；每日次数在锁内读取与累加；重复投递的命令最先被识别为重放 |
| `/bribe`（`economy/operations/bribe.py`，命令当前禁用） | 扣款 | `bribe:<chat>:<msg>` | 扣款与好感度写入同事务 |
| `/btc_predict` 下注（`crypto/crypto_predict.py` `create_prediction`） | 扣款 | `btc:<uid>:<开始时间>:bet` | 扣款先于写入，与预测记录同事务；余额不足不留预测 |
| 预测结算（`check_prediction_result`） | 中奖入账 | `btc:<uid>:<开始时间>:win` | 取价在事务外；标记完成与入账同事务，锁内重新确认预测仍未结算 |
| 过期未结算预测（`create_prediction` 内） | 退回下注 | `refund:btc:<uid>:<开始时间>:bet`（旧预测兜底 `…:expired`） | 退款、标记完成与新一轮下注同事务 |
| `/swap`（`crypto/swap_fogmoe_solana_token.py` `submit_swap_request`） | 扣款 | `swap:<chat>:<msg>` | 扣款与 `token_swap_requests` 记录同事务；待处理请求与余额都在锁内确认 |
| AI `kindness_gift` 工具（`ai/tools/user_tools.py` `grant_kindness`） | 入账 | `kindness:<uid>:<上一次赠币时间>` | 先锁收款人，冷却检查（数据库时钟）、入账、`kindness_gifts` 记录同事务 |

### 游戏（`features/games/`）

| 文件 | 操作 | op_key | 事务边界 |
|---|---|---|---|
| `games/gamble_rounds.py` | 下注 | `gamble:<round_id>:bet:<uid>` | 锁轮次行 → 校验面板与开放状态 → 登记 `gamble_bets` → 扣款，同事务 |
| `games/gamble_rounds.py` | 结算 / 中奖者缺失时退款 | `gamble:<round_id>:payout` / `refund:gamble:<round_id>:bet:<uid>` | 锁轮次行 → 读下注 → 入账或逐笔退款 → 轮次状态转换，同事务 |
| `games/rps_games.py` | 建局与双方入场费 | `rps:<game_id>:entry:<uid>` | 按 id 升序锁双方 user 行 → 建局 → 两笔扣款，同事务 |
| `games/rps_games.py` | 结算 / 平局、超时、创建失败退款 | `rps:<game_id>:win` / `refund:rps:<game_id>:entry:<uid>` | 锁对局行，状态仍为 `choosing` 才转换，余额变动同事务 |
| `games/sicbo.py` | 下注与奖金 | `sicbo:<chat_id>:<message_id>:bet` / `:win` | 扣款与入账同事务 |
| `games/omikuji.py` | 抽签扣费 | `omikuji:<uid>:<日期>` | 锁用户 → 检查当天记录 → 扣费 → 登记签文，同事务 |
| `games/rpg/settlement.py` | 回血 / 怪物奖励 / PvP | `rpg:heal:…`、`rpg:monster:…:reward`、`rpg:pvp:…:loss` / `:win` | 余额变动与角色状态同事务；PvP 双方同事务 |

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

## 旧接口已移除

`process_user` 的 `add_free_coins`、`add_paid_coins`、`spend_user_coins`、`update_user_coins`、
`async_update_user_coins`，以及 `stake_reward_pool` 的 `add_to_pool`、`subtract_from_pool` 已经删除，没有保留兼容层。
`modules/` 里既没有它们的定义也没有调用，`tests/test_games_balance_boundary.py` 会在它们重新出现时失败。替代：

| 已删除的函数 | 现在用 |
|---|---|
| `add_free_coins` | `balance.credit` |
| `add_paid_coins` | `balance.credit(..., kind=CoinKind.PAID)` |
| `spend_user_coins` | `balance.debit`，用异常区分余额不足与用户不存在 |
| `update_user_coins`、`async_update_user_coins` | `balance.credit` / `balance.debit` |
| `stake_reward_pool.add_to_pool` / `subtract_from_pool` | `credit_pool` / `debit_pool` |

升级前旧版本留下的账本记录里，`reason` 以 `legacy:` 开头的行是旧接口的产物（每次调用一个一次性 op_key），之后不会再有新行；
对账规则不受影响。

没有不经过余额服务的变动：新用户注册（`/me`、邀请注册）以余额 0 开户，再用 `signup:<uid>` 把开户奖励
记进账本。升级到账本之前注册的用户没有 `signup` 记录，他们的开户奖励仍由账本第一行隐含。

## 测试

`tests/integration/` 里的余额、奖池、对话扣费、充值、签到抽奖，以及商店、质押、`/give`、邀请与开户奖励、任务、
BTC 预测、兑换、善意赠币的测试使用真实 MySQL，夹具与运行方式见
[database-migrations.md](database-migrations.md) 的「运行集成测试」。造用户、读账本、Telegram 替身在
`tests/integration/economy_support.py`。纯逻辑（op_key 派生、拆分、套餐规则）的单元测试在
`tests/test_balance_unit.py`；各入口的手续费、保底、权限升级规则与 op_key 派生在
`tests/test_economy_logic.py`。各 repository 的语句级语义在 `tests/integration/test_economy_repositories.py` 与
`test_game_repositories.py`，适配层的回复映射在 `tests/test_shop_handlers.py` 与 `tests/test_economy_handlers.py`。
