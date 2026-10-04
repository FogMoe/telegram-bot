"""游戏的持久化：只有 SQL，没有玩法规则、没有 Telegram、不碰余额。

约定（对应 docs/architecture.md 的「经济与游戏的分层」）：

- 第一个参数是调用方事务的 `connection`：事务和锁顺序由业务操作（`gamble_rounds`、`rps_games`、
  `rpg/settlement`、`omikuji`）持有，repository 不开事务、不提交，所以状态转换能和余额变动
  在同一个事务里提交或回滚。`for_update=True` 的读取就是加锁读，调用方据此决定锁的顺序。
- 返回 dataclass 或基础类型，不把 `Row` 交给调用方。
- 持久化的状态取值（`open` / `settled` / `choosing` 等）与记录类型定义在这里，业务操作模块导入使用。
- 金币只通过 `core.balance` 变动，这里没有任何余额写入。

模块：`gamble`（多人下注的轮次与下注）、`rps`（石头剪刀布对局）、`omikuji`（御神签记录）、
`rpg`（角色、装备、道具与战斗经验）。
"""
