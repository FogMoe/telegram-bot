"""经济功能的持久化：只有 SQL，没有业务规则、没有 Telegram。

约定（对应 docs/architecture.md 的「经济与游戏的分层」）：

- 写入函数的第一个参数是调用方事务的 `connection`：事务由业务操作（`features/economy/operations`）持有，
  repository 不开事务、不提交，所以一组写入可以和余额变动在同一个事务里提交或回滚。
- 读取函数的 `connection` 是可选的：传入时在调用方的事务里读（拿到 user 行锁之后的第一次一致性读
  能看到上一个持锁者提交的值），不传时用一次性的连接。
- 返回 dataclass 或基础类型，不把 `Row` 交给调用方。
- 金币只通过 `core.balance` 变动；这里读取余额相关数据时只读。

每个模块对应一个功能：`shop`、`stake`、`invitations`、`tasks`、`checkin`、`coins`（赠送与富豪榜）、
`charge`（卡密、充值请求）、`web_passwords`、`lottery`。
"""
