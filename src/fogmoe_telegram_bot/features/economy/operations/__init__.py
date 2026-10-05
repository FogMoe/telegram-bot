"""经济功能的业务操作：规则、事务与余额变动，不依赖 Telegram。

约定（对应 docs/architecture.md 的「经济与游戏的分层」）：

- 输入是类型化的请求或普通参数，结果是 dataclass / 枚举，不返回给用户看的文案、不 import `telegram`。
  文案与按钮由 Telegram 适配层（`features/economy/*.py`）和展示模块（`shop_views.py`）负责。
- 事务由操作自己持有：改动余额的操作都用 `balance.run_in_transaction` 开一个事务，业务状态
  （`repositories`）与余额变动在同一个事务里提交或回滚，所以失败不需要退款；死锁时整个事务重跑，
  `work` 里不做事务之外的副作用。
- 余额只通过 `core.balance` 变动，`op_key` 由持久身份派生，见 docs/balance-service.md。
- SQL 都在 `features/economy/repositories`，这里不出现 SQL 字符串。
"""
