# 测试规范

本项目测试以高价值核心逻辑为主，不追求全覆盖。优先保证容易回归、影响用户体验、外部依赖较多或分支复杂的代码有小而稳定的测试。

## 测试目标

- 覆盖核心纯逻辑，例如 AI 消息内容降级、token 估算、回复过滤、金额/奖励/状态计算等。
- 覆盖容易被配置或 provider 切换影响的分支。
- 覆盖 bug 修复对应的最小复现场景，避免同类问题回归。
- 不为了覆盖率去测试 Telegram 框架、数据库驱动、第三方 SDK 的内部行为。

## 分层约定

- `modules/main.py` 只作为进程入口，不写单元测试。
- `modules/app/` 是应用组装和 Telegram handler 注册层，测试重点放在较稳定的组装边界，避免启动真实 bot。
- `modules/core/` 放跨功能共享逻辑，适合写小型单元测试。
- `modules/features/` 放业务功能。优先把可测试的纯逻辑拆到独立函数或小模块，再让 Telegram handler 调用它们。
- 外部服务调用、数据库读写、Telegram API 交互默认用替身对象或小范围集成测试，不在普通单元测试里访问真实网络或真实数据库。

## 测试选择标准

优先写这些测试：

- 输入输出清晰的纯函数。
- 复杂分支、fallback、边界值、异常路径。
- 曾经出过问题或改动频繁的逻辑。
- 用户可见影响大的核心路径，例如 AI 回复过滤、多模态消息降级、token 限额估算、经济系统记账规则。

可以暂缓这些测试：

- 只有一行框架注册代码的 handler。
- 纯粹转发到第三方 SDK 的薄封装。
- 短期会频繁改版且尚未稳定的实验功能。
- 只能靠真实 Telegram、真实支付、真实 AI provider 才能验证的路径。

## 编写规范

- 默认使用 `pytest`，测试代码保持轻量，优先使用普通 `assert`。
- 测试文件放在 `tests/`，命名为 `test_*.py`。
- 每个测试聚焦一个行为，断言结果而不是实现细节。
- 测试数据尽量小，避免读取 `.env`、真实资源文件或网络。
- 新增业务逻辑时，优先让核心判断函数不依赖 Telegram Update、数据库 session 或外部 client。
- 需要替身对象时，用简单 fake/stub 类，不引入复杂 mock 层。

## 运行方式

在 Windows 上使用项目虚拟环境：

```powershell
.\.venv\Scripts\python.exe -m pytest
```

手动检查当前 `.env` 里的真实 AI API 连通性：

```powershell
$env:RUN_ENV_API_CONNECTIVITY_TESTS = "1"
.\.venv\Scripts\python.exe -m pytest tests/test_env_api_connectivity.py -s
```

默认会按 `AI_CHAT_ORDER` 检查 chat provider。只检查指定 provider 时：

```powershell
$env:RUN_ENV_API_CONNECTIVITY_TESTS = "1"
$env:ENV_API_CONNECTIVITY_PROVIDERS = "gemini"
.\.venv\Scripts\python.exe -m pytest tests/test_env_api_connectivity.py -s
```

开发依赖安装：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```
