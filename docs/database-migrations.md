# 数据库迁移

本页是迁移的操作手册：全新安装、既有安装升级、部分失败后的恢复、编写新迁移的规则，以及如何运行 MySQL 集成测试。

- 迁移脚本在 `alembic/versions/`，运行入口是 `alembic/env.py`。
- 版本表、数据库 URL 优先级和可重入 DDL 助手在 `src/fogmoe_telegram_bot/core/migration_support.py`。
- 当前的 head 用 `alembic heads` 查看，应当始终只有一个。
- 要求 MySQL 8.0.4 及以上（0018 使用 `REGEXP_REPLACE`），迁移账号需要 DDL 权限。

## 全新安装

按 README 的「创建数据库」建好 `utf8mb4` 的空库，在 `.env` 里配置连接信息，然后：

```bash
uv run alembic upgrade head
```

连接信息来自 `src/fogmoe_telegram_bot/core/config.py` 中的 `SQLALCHEMY_DATABASE_URI`（`DATABASE_URL` 或 `MYSQL_*`）。

全新库在 strict 模式下可以直接迁移到 head，之后可以写入第一条对话，也可以反复更新抽奖日期。

## 数据库 URL 的优先级

`alembic/env.py` 的 `get_url()` 按以下顺序决定连接哪个库：

1. `config.attributes["db_url"]`：程序化调用，测试使用。
2. 命令行 `-x db_url=<url>`。
3. 应用配置 `SQLALCHEMY_DATABASE_URI`（读取 `.env`）。
4. `alembic.ini` 的 `sqlalchemy.url`。

只要前两项之一存在，就不会读取应用配置。对一个临时库升级或 stamp 时用 `-x`：

```bash
uv run alembic -x db_url='mysql+asyncmy://user:pass@host:3306/dbname?charset=utf8mb4' upgrade head
```

## 既有安装升级

### 升级前

1. **停掉机器人。** 0017 会重建 `chat_records` 的 `id` 列并给两张表加约束，期间不应有写入。
2. **备份。** `mysqldump --single-transaction <库名> > backup.sql`。
3. **预览将被处理的数据。**

```sql
-- 重复的对话：每个 conversation_id 只保留一行，其余进入 chat_records_dedup_0017
SELECT conversation_id, COUNT(*) FROM chat_records GROUP BY conversation_id HAVING COUNT(*) > 1;
-- 重复的抽奖记录：每个 user_id 保留 last_lottery_date 最大的一行，其余进入 user_lottery_dedup_0017
SELECT user_id, COUNT(*) FROM user_lottery GROUP BY user_id HAVING COUNT(*) > 1;
-- 将被 0018 删除的 Web 密码（不是 Argon2id 的全部删除）
SELECT COUNT(*) FROM web_password WHERE LEFT(password, 10) <> BINARY '$argon2id$';
-- 当前记录的版本
SELECT version_num FROM alembic_version;
```

如果表里有 `0000-00-00` 之类的非法日期（非 strict 模式遗留），重建表时会在 strict 会话里报错，需要先手工修正这些值。

### 升级

```bash
uv run alembic upgrade head
```

升级自动完成这些事：

- **版本表加宽。** 运行任何迁移之前，`alembic_version.version_num` 被改成 `VARCHAR(255)`（不存在则直接按这个宽度创建）。默认的 `VARCHAR(32)` 装不下几个已有 revision ID。已记录的 revision ID 一律不改。
- **还原被截断的版本号。** 旧的 `VARCHAR(32)` 版本表在非 strict 模式下可能把超长 ID 截断成 32 位。截断值恰好只对应一个已知 revision 时，会被改写成完整 ID；对应不上的值保持原样，由 alembic 报错。
- **0017 `schema_contracts`。**
  - `chat_records.id` 变成 `BIGINT AUTO_INCREMENT` 主键，`conversation_id` 加唯一索引 `uq_chat_records_conversation_id`。应用从不读取 `chat_records.id`，存量行重新编号。
  - `user_lottery.user_id` 变成主键。
  - 迁移前先检查实际 schema：已经存在恰好覆盖目标列的主键或唯一索引（不论索引名）就跳过对应步骤，也不会创建备份表。表上已有别的主键时，改用唯一索引。
- **0018 `privacy_retention`。**
  - 删除 `web_password` 中不以 `$argon2id$` 开头的行。受影响的用户用 `/webpassword` 重新设置。
  - 把 `chat_records_group` 里 `/charge`、`/webpassword`（含 `/charge@BotName`，命令名大小写不敏感）的参数替换成 `[redacted]`，命令本身保留。文本消息直接处理；非文本消息先解 base64、脱敏、再编码回去。
  - 降级不会恢复被删除或脱敏的数据。
- **0019 `coin_ledger`。** 新建 `coin_ledger`、`stake_pool_ledger`、`topup_requests`，不改动已有表。
  新版本的所有余额操作都依赖这三张表，所以先迁移、再启动新版本。账本从升级后开始记录，
  升级前的余额由每个用户的第一行隐含，对账方法见 [balance-service.md](balance-service.md)。
  升级前发出的管理员充值按钮会失效：点击只提示让用户重新发起，不会入账。
- **0021 `game_state`。** 新建 `gamble_rounds`、`gamble_bets`、`rps_games`，不改动已有表。
  新版本的下注与石头剪刀布依赖这三张表，所以先迁移、再启动新版本。旧版本放在内存里的进行中的下注轮次与对局
  不会迁移：它们随旧进程消失，已扣的金币不会自动退还，升级尽量选在没有进行中的游戏时，
  详见 [job-recovery.md](job-recovery.md) 的「升级时正在进行的游戏」。旧版本发出的下注与选择按钮升级后失效。

0018 删除的是旧哈希。先部署会写入 Argon2id 的代码，再执行 0018；否则迁移之后用旧代码设置的 SHA-256 哈希会再次留在库里。

### 备份表的处置

去重时被淘汰的行搬进备份表，而不是直接删除：

| 表 | 内容 | 对账线索 |
|---|---|---|
| `chat_records_dedup_0017` | 被淘汰的对话行（含完整 `messages`） | `kept_id` 指向保留下来的那一行 |
| `user_lottery_dedup_0017` | 被淘汰的抽奖行 | `user_id` 对应保留下来的行；`row_id` 是迁移期间临时编号 |

保留规则：

- 对话：`last_rotated_at` 最新（NULL 视为最旧），其次 `timestamp` 最新，最后 `id` 最大。应用每次更新都写同一 `conversation_id` 的所有行，所以重复行只会在创建后从未被更新时内容不同，较晚创建的那行是较新的状态。
- 抽奖：`last_lottery_date` 最大（NULL 视为最旧）。

处置步骤：抽查备份表，确认没有需要合并回去的对话；备份表里是用户的对话原文，确认完后尽快删除，不要长期保留。

```sql
DROP TABLE IF EXISTS chat_records_dedup_0017, user_lottery_dedup_0017;
```

### 升级后检查

```sql
SHOW CREATE TABLE chat_records;   -- id bigint AUTO_INCREMENT PRIMARY KEY，conversation_id 有唯一索引
SHOW CREATE TABLE user_lottery;   -- user_id 是主键
SELECT version_num FROM alembic_version;
SHOW CREATE TABLE alembic_version; -- version_num varchar(255)
```

## 部分失败后的恢复

MySQL 的 DDL 会隐式提交。迁移中途失败时，已执行的 DDL 保留，而 `alembic_version` 没有更新，库就停在「schema 已部分或全部应用、版本号还在前一个 revision」。

`env.py` 对每个 revision 单独提交（`transaction_per_migration=True`），版本号更新紧跟在该 revision 的 DDL 之后。

**所有 revision 都是可重入的**：建表用 `CREATE TABLE IF NOT EXISTS`，加列和加索引前先查 `information_schema`，数据回填写成幂等形式。恢复时直接重跑：

```bash
uv run alembic upgrade head
```

回填的幂等规则：

- `0002_stake_reward_pool`：奖池行已存在时保留原余额。
- `0005`：`ai_user_diary` 已被 0006 删除时跳过回填；第 1 页已存在时不覆盖。
- `0012`：只回填仍是默认值 `free` 的行，不覆盖之后变化的套餐。

### 什么时候需要 stamp

只有在重跑也无法通过时才使用 stamp，典型情况是手工改过的 schema 与迁移的预期冲突（例如同名列类型不同）。步骤：

1. 对照下表，确认库里已经有目标 revision 产生的全部对象，缺的手工补齐。
2. `alembic stamp <revision>`（可加 `-x db_url=...`）。stamp 只写版本号，不执行任何 DDL 或数据回填。
3. `alembic upgrade head` 继续后面的 revision。

| revision | 产生的对象 | stamp 前需要手工完成的数据步骤 |
|---|---|---|
| `0001_initial` | 全部初始表 | — |
| `0002_add_chat_records_last_rotated_at` | `chat_records.last_rotated_at` | — |
| `0002_stake_reward_pool` | `stake_reward_pool` 与 `id=1` 的初始行 | 缺行时 `INSERT INTO stake_reward_pool (id, balance) VALUES (1, 0)` |
| `0003_add_ai_user_diary` / `0006_drop_ai_user_diary` | `ai_user_diary` 的创建与删除 | — |
| `0004_add_ai_schedules` | `ai_schedules` | — |
| `0005_add_ai_user_diary_pages` | `ai_user_diary_pages` | `ai_user_diary` 还存在时，把非空内容回填为第 1 页（`INSERT ... SELECT ... ON DUPLICATE KEY UPDATE` 不覆盖已有页） |
| `0007_add_user_permanent_records_limit` | `user.permanent_records_limit` | — |
| `0009_add_recharge_blocked_until` | `user.recharge_blocked_until` | — |
| `0010_add_user_give_daily` | `user_give_daily` | — |
| `0011_add_user_coins_paid` | `user.coins_paid` | — |
| `0012_add_user_plan` | `user.user_plan` | `UPDATE user SET user_plan='paid' WHERE coins_paid > 0 AND user_plan='free'`，再把 `ADMIN_USER_ID` 对应的行设为 `admin` |
| `0013_add_ai_schedule_recurrence` | `ai_schedules.recurrence_unit` / `recurrence_interval` / `last_run_at` | — |
| `0014_add_ai_user_diary_page_index` | `ai_user_diary_pages.title` / `summary` | — |
| `0015_add_ai_idle_followups` | `ai_idle_followups` | — |
| `0016_add_ai_schedule_daily_limit` | `user.ai_schedule_trigger_date` / `ai_schedule_trigger_count` | — |
| `0017_schema_contracts` | 见「升级后检查」 | 重复数据的对账见「备份表的处置」 |
| `0018_privacy_retention` | 无新对象 | 删除旧密码哈希、脱敏群聊历史（SQL 在迁移文件里） |
| `0019_coin_ledger` | `coin_ledger`、`stake_pool_ledger`、`topup_requests` | — |
| `0020_job_claims` | `ai_schedules` / `ai_idle_followups` 的 `claim_token` / `claim_attempts` / `stage`（`ai_schedules` 另有 `claim_until`）、索引 `idx_ai_schedules_claim`、表 `ai_job_attempts` | 旧版本留下的 `executing` 行：`stage` 设为 `generating`，`ai_schedules.claim_until` 设为当前时间（语义见 [job-recovery.md](job-recovery.md)） |
| `0021_game_state` | `gamble_rounds`、`gamble_bets`、`rps_games` | — |

## 离线 SQL

`alembic upgrade head --sql` 可以生成 SQL。离线模式无法查询数据库，所有助手都按「对象尚不存在」输出完整语句，因此生成的脚本只适用于标准 schema 的库；它的开头会按需创建或加宽版本表。已经手工改过 schema 的库请直接在线升级。

## 编写新迁移

- 新 revision 的 DDL 必须可重入，使用 `migration_support` 里的助手（`add_columns_if_missing`、`add_unique_key_if_missing`、`drop_index_if_exists`，建表用 `IF NOT EXISTS`）；助手放在 `src/fogmoe_telegram_bot/core/`，不要放进 `alembic/versions/`，那里的每个 .py 都会被当成 revision。
- 一个 revision 里有多条会隐式提交的语句时，让每条语句都能独立重跑；数据回填写成重跑不会覆盖较新数据的形式。
- 不要修改已发布的 revision ID。
- `tests/integration/test_migrations_mysql.py` 中的回放测试会把版本号回退到图中的每一个 revision 再升级，要求 schema 与数据保持不变；新 revision 会自动被覆盖。

## 运行集成测试

集成测试在 `tests/integration/`，连接真实 MySQL。设置 `TEST_MYSQL_URL`（服务器地址，不带库名）才会运行。未设置时，不带路径参数的 `pytest` 不收集这个目录，显式指定 `tests/integration` 时整个目录 skip。

```bash
# Linux / macOS
TEST_MYSQL_URL=mysql+asyncmy://root@127.0.0.1:3306 uv run pytest tests/integration
```

```powershell
# Windows PowerShell
$env:TEST_MYSQL_URL = "mysql+asyncmy://root@127.0.0.1:3306"
uv run pytest tests/integration
```

约定：

- 账号需要 `CREATE` / `DROP` 数据库的权限。每个测试使用唯一库名 `it_<hex>`，结束后删除，只删除测试自己创建的库。
- 会话统一追加 `STRICT_TRANS_TABLES`，不依赖服务器默认的 `sql_mode`。
- 应用和 alembic 只使用显式传入的测试 URL。集成测试期间配置里的数据库地址被换成不可达的占位值，任何回落到 `.env` 的路径都会连接失败。
- CI 用 `mysql:8.4` service 容器提供同名环境变量。

夹具的 API 写在 `tests/integration/conftest.py` 的模块 docstring 里，函数写在 `tests/integration/mysql_support.py`；最小示例见 `tests/integration/test_fixture_smoke.py`。不需要数据库的迁移检查（revision 图、离线 SQL、URL 优先级）在 `tests/test_migration_support.py`，随普通 `pytest` 运行。
