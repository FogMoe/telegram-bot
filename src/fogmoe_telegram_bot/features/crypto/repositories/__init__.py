"""加密货币功能里持有金币的两个入口（BTC 价格预测、$FOGMOE 兑换）的持久化：只有 SQL。

约定与 `features/economy/repositories` 相同：写入函数的第一个参数是调用方事务的 `connection`，
事务与余额变动由业务操作持有；读取函数的 `connection` 可选；不 import `telegram`、不碰余额服务。
见 docs/architecture.md 的「经济与游戏的分层」。
"""
