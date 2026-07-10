<h1 align="center">雾萌 · 多功能 Telegram 机器人</h1>

<div align="center">

![License](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)
![Python](https://img.shields.io/badge/python-3.10+-green.svg)
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

- Python 3.10 或更高版本
- MySQL 8.0 或更高版本
- Linux、macOS 或 Windows

### 1. 获取代码并安装依赖

<details>
<summary>Linux / macOS</summary>

<br>

```bash
git clone https://github.com/FogMoe/telegram-bot.git
cd telegram-bot
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```

</details>

<details>
<summary>Windows PowerShell</summary>

<br>

```powershell
git clone https://github.com/FogMoe/telegram-bot.git
Set-Location telegram-bot
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

</details>

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

<details>
<summary>Linux / macOS</summary>

<br>

```bash
.venv/bin/python -m alembic upgrade head
.venv/bin/python modules/main.py
```

</details>

<details>
<summary>Windows PowerShell</summary>

<br>

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe modules/main.py
```

</details>

启动后，在 Telegram 中向机器人发送 `/start` 或 `/help` 即可开始使用。

### Linux 后台管理脚本

也可以使用仓库中的脚本管理后台进程：

```bash
chmod +x runBot.sh
./runBot.sh init
# 配置 .env，并按照前文完成数据库迁移
./runBot.sh start
./runBot.sh status
./runBot.sh restart
./runBot.sh stop
```

脚本会自行管理 `venv/` 虚拟环境，因此它与前面的 `.venv` 手动安装方式二选一即可。

---

## 🐳 Docker 部署

当前 Docker 镜像只运行机器人，**不包含 MySQL，也不会自动执行数据库迁移**。请先准备外部 MySQL、填写 `.env`，并在宿主机完成 Alembic 迁移。

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

## 🧱 技术栈

- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot)：Telegram Bot API
- [LiteLLM](https://github.com/BerriAI/litellm)：统一 AI provider 调用与 fallback
- [SQLAlchemy](https://www.sqlalchemy.org/) + [asyncmy](https://github.com/long2ice/asyncmy)：异步数据库访问
- [Alembic](https://alembic.sqlalchemy.org/)：数据库迁移
- [MySQL](https://www.mysql.com/)：业务数据存储
- Docker Compose：容器化运行

---

## 🤝 开发与贡献

欢迎提交 Bug 报告、功能建议和代码贡献。开发依赖位于 `requirements-dev.txt`，测试使用 `pytest`。

测试范围、编写约定、AI provider 的配置与路由设计和运行方式见文档 [docs](docs)。 

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
