<h1 align="center">雾萌 · 多功能 Telegram 机器人</h1>

<div align="center">

![License](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)
![Python](https://img.shields.io/badge/python-3.13-green.svg)
![Telegram Bot](https://img.shields.io/badge/Telegram-Bot-blue.svg)
[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/FogMoe/telegram-bot)

集 AI 助手、社区积分、娱乐互动和群组管理于一体的 Telegram 机器人。

[体验机器人](https://t.me/FogMoeBot) · [查看命令说明](resources/telegram_help.md)

</div>

---

## ✨ 功能亮点

### 🤖 AI 对话与智能工具

- **多模型路由**：通过 LiteLLM 接入 OpenAI、Google Gemini、Azure OpenAI、智谱 AI（Z.ai）和 SiliconFlow，可按优先级自动切换备用服务。
- **私聊与群聊**：支持文字、图片和贴纸消息；群聊中可结合上下文判断是否参与对话。
- **个性化互动**：用户可以设置个人信息，机器人会维护好感度、长期印象、对话摘要和个人日记。
- **多模态能力**：支持图片理解，并可按配置生成图片、语音和贴纸回复。
- **联网与执行工具**：可按需调用搜索、网页读取、Python 执行和隔离 Linux 沙箱等工具。
- **定时消息**：支持创建、查看和取消一次性或周期性的 AI 私聊提醒。
- **空闲跟进**：私聊暂时中断后，可按用户近期对话节奏生成短期回顾，并由 AI 判断是否自然跟进一次。

AI 搜索、代码执行、图片生成和语音生成等扩展能力需要配置对应的第三方服务；未配置时不影响其他基础功能。

### 💰 社区积分与成长

- 每日签到、每日奖励、任务和邀请奖励
- 用户资料、虚拟金币赠送、互动奖励和排行榜
- 虚拟商城、金币锁定奖励、兑换码和管理员积分服务
- 面向群组互动的虚拟积分体系，可与 AI 好感度和部分娱乐功能联动

### 🎮 娱乐互动

- 御神签和每日运势
- 石头剪刀布、多人金币小游戏和骰子挑战
- 行情预测小游戏
- RPG 文字冒险
- 随机图片与音乐搜索

以上玩法仅使用机器人内的虚拟积分，供社区互动娱乐，不支持提现，也不构成投资或收益承诺。

### 👥 群组管理

- 新成员验证，降低机器人和垃圾账号干扰
- 垃圾消息检测与管制
- 关键词自动回复
- 群成员举报与管理员处理
- 群聊上下文记忆和智能回复触发

### 🧰 实用功能

- 中英互译
- 加密货币价格图表
- Web 登录密码设置
- 管理员运行状态、日志和公告工具

---

## 📖 常用命令

机器人内发送 `/help` 可查看当前部署启用的完整命令。常用入口包括：

- AI 对话：`/fogmoebot`、`/setmyinfo`、`/clear`
- 个人与积分：`/me`、`/checkin`、`/lottery`、`/task`、`/shop`、`/give`、`/rich`、`/stake`、`/ref`、`/charge`
- 娱乐：`/omikuji`、`/rps_game`、`/gamble`、`/sicbo`、`/btc_predict`、`/rpg`
- 群组管理：`/verify`、`/spam`、`/keyword`、`/report`
- 实用工具：`/tl`、`/music`、`/pic`、`/chart`

部分命令仅适用于群聊、管理员或已配置相应第三方服务的部署。

---

## 🚀 快速开始

### 环境要求

- Python 3.13（版本范围以 [pyproject.toml](pyproject.toml) 的 `requires-python` 为准）
- MySQL 8.0 或更高版本
- Linux、macOS 或 Windows

### 1. 获取代码并安装依赖

先安装 [uv](https://docs.astral.sh/uv/)。依赖版本锁定在 `uv.lock`，`uv sync` 按它在仓库根目录生成 `.venv`。

```bash
git clone https://github.com/FogMoe/telegram-bot.git
cd telegram-bot
uv sync --no-dev
```

复制配置模板：Linux / macOS 执行 `cp .env.example .env`，Windows PowerShell 执行 `Copy-Item .env.example .env`。

### 2. 创建数据库

登录 MySQL：

```bash
mysql -u root -p
```

创建使用 `utf8mb4` 的数据库：

```sql
CREATE DATABASE fogmoe_telegram_bot_db
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_general_ci;
```

### 3. 配置环境变量

编辑刚刚创建的 `.env`。运行机器人至少需要：

- `TELEGRAM_BOT_TOKEN`：从 [@BotFather](https://t.me/BotFather) 获取
- `ADMIN_USER_ID`：部署者自己的 Telegram 数字用户 ID
- `MYSQL_HOST`、`MYSQL_PORT`、`MYSQL_USER`、`MYSQL_PASSWORD`、`MYSQL_DATABASE`
- 至少一套可用的 AI provider API Key 和对应模型

主聊天、摘要、翻译、图片理解和群聊判断可以分别选择 provider。未使用的 provider 应从聊天顺序和任务配置中移除。所有配置项及注释均以 [.env.example](.env.example) 为准。

### 4. 初始化数据库并启动

```bash
uv run alembic upgrade head
uv run python modules/main.py
```

升级既有安装、迁移中途失败后的恢复和集成测试的运行方式见 [docs/database-migrations.md](docs/database-migrations.md)。

启动后，在 Telegram 中向机器人发送 `/start` 或 `/help` 即可开始使用。需要在服务器上长期运行时，用下面的 Docker 部署。

---

## 🐳 Docker 部署

当前 Docker 镜像只运行机器人，**不包含 MySQL，也不会自动执行数据库迁移**。请先准备外部 MySQL、填写 `.env`，再执行 Alembic 迁移（宿主机或容器内均可，镜像包含迁移脚本）。镜像按 `uv.lock` 安装依赖，基础镜像与 `requires-python` 使用同一个 Python 版本。

在容器内执行迁移：

```bash
docker compose run --rm bot python -m alembic upgrade head
```

```bash
docker compose build bot
docker compose up -d bot
docker compose logs -f bot
```

更新代码后重建：

```bash
git pull --ff-only
docker compose up -d --build bot
```

如果 MySQL 位于 Docker 宿主机，`MYSQL_HOST` 必须填写容器能够访问的地址。Docker Desktop 通常可使用 `host.docker.internal`；Linux 服务器请使用实际可访问的主机名或 IP。

---

## 📋 日志

应用日志始终写入仓库下的 `logs/tgbot.log`（管理员的 `/logs` 命令也读取它）。是否同时输出到 stdout 由 `LOG_TO_STDOUT` 控制，日志级别由 `LOG_LEVEL` 控制，两者见 [.env.example](.env.example)。

| 运行方式 | 实时查看 | 持久文件 |
| --- | --- | --- |
| `uv run python modules/main.py` | 控制台 | `logs/tgbot.log` |
| Docker Compose | `docker compose logs -f bot` | 宿主机的 `./logs/tgbot.log`（Compose 默认把 `./logs` 挂载到容器的 `/app/logs`） |

保留策略：

- **轮转文件**：`tgbot.log` 写满后轮转为 `tgbot.log.1`、`tgbot.log.2`……，只保留有限份数，轮转参数见 `modules/core/bot_logging.py` 的 `build_handlers`。重启不会清空这些文件。
- **容器重启或重建**：`./logs` 在宿主机上，`docker compose restart`、`docker compose up -d --build` 之后日志文件仍在。
- **`docker compose logs`**：由 Docker 的 json-file 驱动保存，大小和份数限制见 `docker-compose.yml` 的 `logging`。容器被删除（例如重建）后这部分输出随之丢失，需要更长留存时请查看 `./logs`，或接入自己的日志收集系统。

---

## ⚙️ 运行时与容量

AI 对话是原生 async：模型调用与数据库工具都在事件循环里 `await`，只有必须同步的 SDK（requests、e2b 等）放进有界线程池。
对话有明确的容量与超时策略，默认值适合单进程小中型部署，需要时在 `.env` 里调整（均有默认值，见 [.env.example](.env.example) 的「运行时」一节）：

| 配置 | 默认 | 作用 |
| --- | --- | --- |
| `CHAT_MAX_CONCURRENT_TURNS` | 32 | 同时运行的对话轮次上限 |
| `CHAT_MAX_QUEUED_TURNS` / `CHAT_QUEUE_MAX_WAIT_SECONDS` | 32 / 20 | 排队的长度与最长等待；超过后用户会收到「繁忙」提示，**不扣硬币** |
| `CHAT_MAX_PENDING_PER_USER` | 3 | 同一用户同时「处理中 + 排队」的轮次上限 |
| `CHAT_TURN_DEADLINE_SECONDS` | 360 | 整轮截止时间，覆盖排队、provider 回退、工具与投递；到期后取消剩余工作并提示用户（已扣的硬币不退） |
| `TELEGRAM_CONCURRENT_UPDATES` | 128 | Telegram 同时处理的 update 数 |
| `BLOCKING_TOOL_THREADS` | 8 | 同步工具的线程池大小 |

指标（排队深度、排队延迟、整轮耗时、超时、provider 与工具失败）每 5 分钟写一行日志：`grep "runtime metrics" logs/tgbot.log`。
执行模型、过载行为、关停顺序与基准结果见 [docs/runtime.md](docs/runtime.md)。

---

## 🧱 技术栈

- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot)：Telegram Bot API
- [LiteLLM](https://github.com/BerriAI/litellm)：统一 AI provider 调用与 fallback
- [SQLAlchemy](https://www.sqlalchemy.org/) + [asyncmy](https://github.com/long2ice/asyncmy)：异步数据库访问
- [Alembic](https://alembic.sqlalchemy.org/)：数据库迁移
- [MySQL](https://www.mysql.com/)：业务数据存储
- Docker Compose：容器化运行

---

## 🤝 开发与贡献

欢迎提交 Bug 报告、功能建议和代码贡献。

测试范围、编写约定、运行方式、静态检查和类型检查见 [docs/testing-guidelines.md](docs/testing-guidelines.md)，AI provider 的配置与路由设计见 [docs](docs)。提交到 GitHub 的变更由 [CI](.github/workflows/ci.yml) 运行同样的检查。

### 依赖管理

依赖声明在 [pyproject.toml](pyproject.toml)，`uv.lock` 是唯一的版本来源。开发依赖（pytest、ruff、mypy）在 `dev` 依赖组，`uv sync` 默认一并安装。用 `uv add <包>`、`uv add --dev <包>`、`uv remove <包>` 修改依赖，它们会同时更新 `pyproject.toml` 和 `uv.lock`；CI 用 `uv lock --check` 检查两者一致。

`modules/` 里直接 import 的第三方包必须写进 `pyproject.toml` 的 `dependencies`，不能只依赖传递依赖。

---

## 🔒 安全与隐私

- 请勿把包含真实密钥的 `.env` 提交到版本库。不要提交 `.env`、数据库备份、运行日志或任何真实 API Key。
- 根据启用的功能，数据库可能保存 Telegram 用户标识、群聊上下文、对话记录和业务数据。
- 启用外部 AI、搜索、代码执行、图片或语音服务前，请审查对应服务的数据与隐私政策。
- 生产环境建议使用权限受限的数据库账号，并定期备份数据库。

---

## 📄 许可证

本项目采用 [GNU Affero General Public License v3.0](LICENSE)。修改并通过网络提供服务时，请遵守 AGPL-3.0 对源代码提供、许可证保留和变更声明的相关要求。

第三方依赖分别遵循其自身许可证。

---

<div align="center">

![GitHub stars](https://img.shields.io/github/stars/FogMoe/telegram-bot?style=social)
![GitHub forks](https://img.shields.io/github/forks/FogMoe/telegram-bot?style=social)

如果这个项目对你有帮助，欢迎点亮 ⭐ Star。

Made with ❤️ by FOGMOE

</div>
