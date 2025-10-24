# 雾萌娘 - 多功能 Telegram 机器人

<div align="center">

![License](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)
![Python](https://img.shields.io/badge/python-3.9+-green.svg)
![Telegram Bot](https://img.shields.io/badge/Telegram-Bot-blue.svg)

一个功能丰富、可扩展的 Telegram 机器人，集成 AI 聊天、经济系统、娱乐游戏和群组管理功能。

[功能特性](#功能特性) • [快速开始](#快速开始) • [部署指南](#部署指南) • [配置说明](#配置说明) • [许可证](#许可证)

</div>

---

## 📋 目录

- [功能特性](#功能特性)
- [快速开始](#快速开始)
- [部署指南](#部署指南)
- [配置说明](#配置说明)
- [使用说明](#使用说明)
- [贡献指南](#贡献指南)
- [许可证](#许可证)
- [联系方式](#联系方式)

---

## ✨ 功能特性

### 🤖 AI 智能聊天
- **多模型支持**：集成 Google Gemini、Azure OpenAI、智谱 AI
- **个性化对话**：可爱、中二、傲娇的"雾萌娘"人设
- **上下文记忆**：支持长期对话记忆和个性化印象
- **好感度系统**：根据互动调整回复风格

### 💰 经济系统
- **金币系统**：签到、任务、邀请获取金币
- **质押机制**：质押金币获得持续收益
- **代币兑换**：支持兑换 Solana 链上 $FOGMOE 代币
- **卡密充值**：管理员可生成充值卡密
- **富豪榜**：展示金币排行榜

### 🎮 娱乐游戏
- **御神签**：每日抽签预测运势
- **猜拳游戏**：经典石头剪刀布
- **赌博系统**：支持多人参与的赌博游戏
- **骰子游戏**：骰宝游戏
- **比特币预测**：模拟加密货币合约预测
- **RPG 文字游戏**：角色扮演冒险游戏

### 👥 群组管理
- **新成员验证**：防止机器人和垃圾账号
- **垃圾消息控制**：智能检测和过滤垃圾内容
- **举报系统**：用户可举报不当消息给管理员
- **关键词自动回复**：自定义关键词触发回复
- **代币图表**：查看加密货币价格图表

### 🛠️ 实用工具
- **中英互译**：快速翻译功能
- **音乐搜索**：搜索并获取音乐资源
- **随机图片**：获取二次元图片
- **邀请系统**：邀请好友获得奖励
- **任务系统**：完成任务获得金币

---

## 🚀 快速开始

### 环境要求

- **Python**: 3.9 或更高版本
- **MySQL**: 8.0 或更高版本
- **操作系统**: Linux / macOS / Windows

### 安装依赖

```bash
# 克隆项目
git clone https://github.com/fogmoe/telegram-bot.git
cd telegram-bot

# 安装 Python 依赖
pip3 install -r requirements.txt
```

### 数据库设置

```bash
# 登录 MySQL
mysql -u root -p

# 创建数据库
CREATE DATABASE fogmoe_telegram_bot_db CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;

# 导入表结构
mysql -u root -p fogmoe_telegram_bot_db < MySQL.sql
```

### 配置环境变量

```bash
# 复制环境变量模板
cp .env.example .env

# 编辑 .env 文件，填入你的配置
nano .env
```

### 启动机器人

```bash
# 方式一：直接运行
cd modules
python3 main.py

# 方式二：使用脚本（后台运行）
./runBot.sh
```

### 停止机器人

```bash
# 查找进程
ps -ef | grep python3

# 终止进程
kill <PID>

# 或使用脚本
# 编辑 runBot.sh 查看停止命令
```

---

## 📦 部署指南

### 使用虚拟环境（推荐）

```bash
# 创建虚拟环境
python3 -m venv venv

# 激活虚拟环境
# Linux/macOS:
source venv/bin/activate
# Windows:
venv\Scripts\activate
```

---

## ⚙️ 配置说明

### 必需配置

#### 获取必要的 API 密钥
在 `.env` `config.py` 文件中配置必需项。

---

## 📖 使用说明

可参考 [@FogMoeBot](https://t.me/FogMoeBot) 或配置文件中的说明进行使用。


### 使用的主要技术

- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) - Telegram Bot API 封装
- [Google Gemini](https://ai.google.dev/) - AI 聊天模型
- [Azure OpenAI](https://azure.microsoft.com/en-us/products/ai-services/openai-service) - AI 服务
- [智谱 AI](https://open.bigmodel.cn/) - 中文 AI 模型
- [MySQL](https://www.mysql.com/) - 数据库

---

## 🤝 贡献指南

我们欢迎所有形式的贡献！
如果发现 Bug 或有功能建议，请报告问题。

---

## 📄 许可证

### AGPL-3.0 License

本项目采用 **GNU Affero General Public License v3.0** 开源协议。

**这意味着：**

⚠️ **您必须：**
- **开源您的修改**：如果您修改了本软件并通过网络提供服务，您必须公开源代码
- **保持相同许可证**：衍生作品必须使用相同的 AGPL-3.0 许可证
- **声明更改**：明确标注您所做的修改
- **提供源代码访问**：向所有通过网络与软件交互的用户提供源代码

🔴 **重要提示：**
- 如果您在服务器上运行修改版本的本软件，并通过网络向用户提供服务（例如作为 Telegram Bot），您**必须**向这些用户提供完整的源代码
- 这是 AGPL 覆盖网络使用场景的主要要求

详细许可证内容请查看 [LICENSE](LICENSE) 文件。

### 第三方许可证

本项目使用的第三方库遵循各自的许可证：
- 依赖库请查看 `requirements.txt`

---

## 🔒 安全与隐私

### 数据安全
- 所有敏感配置使用环境变量管理
- 数据库密码不会硬编码在代码中
- 支持加密存储用户数据

### 隐私保护
- 仅存储必要的用户信息（用户ID、用户名）
- 聊天记录用于提供服务，不会被滥用
- 遵守 Telegram 服务条款和隐私政策

---

## 📊 项目统计

![GitHub stars](https://img.shields.io/github/stars/你的用户名/Multi-Functional-Telegram-Bot?style=social)
![GitHub forks](https://img.shields.io/github/forks/你的用户名/Multi-Functional-Telegram-Bot?style=social)
![GitHub issues](https://img.shields.io/github/issues/你的用户名/Multi-Functional-Telegram-Bot)
![GitHub pull requests](https://img.shields.io/github/issues-pr/你的用户名/Multi-Functional-Telegram-Bot)

---

<div align="center">

**如果这个项目对您有帮助，请给个 ⭐ Star！**

Made with ❤️ by FOGMOE

</div>
