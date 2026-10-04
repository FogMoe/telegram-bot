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
- 永远不要测试或断言 `resources/` 目录中的文件内容、文案、格式或条目，也不要通过配置加载结果间接断言这些内容；测试资源消费逻辑时使用测试内 fixture 或 monkeypatch。
- 测试数据尽量小，避免读取 `.env`、真实资源文件或网络。
- 新增业务逻辑时，优先让核心判断函数不依赖 Telegram Update、数据库 session 或外部 client。
- 需要替身对象时，用简单 fake/stub 类，不引入复杂 mock 层。

## 运行方式

项目虚拟环境是仓库根目录的 `.venv`，用 `uv sync` 或 `python -m venv` 创建都可以，安装步骤见 [README](../README.md)。

在 Windows 上使用项目虚拟环境：

```powershell
.\.venv\Scripts\python.exe -m pytest
```

在 Linux / macOS 上：

```bash
.venv/bin/python -m pytest
```

使用 uv 时也可以直接运行 `uv run pytest`，它会先按 `uv.lock` 同步环境。

### 测试不读取 `.env`

`core/config.py` 在导入时读取配置，`tests/conftest.py` 会在导入项目模块之前设置 `BOT_ENV_FILE=`（空值），所以 pytest 默认不读取仓库根的 `.env`，只使用进程环境变量和代码默认值。需要让测试读取指定文件时，自己设置 `BOT_ENV_FILE=<路径>`。

下面的真实连通性检查是唯一的例外：设置 `RUN_ENV_API_CONNECTIVITY_TESTS=1` 时 conftest 不会屏蔽 `.env`，测试读取其中的真实配置。

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

开发依赖（pytest、ruff、mypy）安装：

```powershell
uv sync
# 或者
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

### MySQL 集成测试

集成测试放在 `tests/integration/`，需要环境变量 `TEST_MYSQL_URL`（形如 `mysql+asyncmy://root:<password>@127.0.0.1:3306`，不含库名），未设置时整体跳过。CI 用 `mysql:8.4` service 提供数据库。普通单元测试不访问真实数据库。

### 启动冒烟检查

不连接 Telegram 和数据库，只确认依赖可导入、Application 能组装、handler 与 job 能注册：

```powershell
.\.venv\Scripts\python.exe modules/main.py --check
```

CI 在构建出的镜像里运行同一条命令，实现见 `modules/app/smoke_check.py`。

### 验证 runBot.sh

`scripts/verify_run_bot.sh` 在临时目录里复制 `runBot.sh`，用替身入口验证 start、status、restart、stop 使用同一套进程识别，包括 PID 文件过期、PID 指向无关进程和旧式启动的进程：

```bash
bash scripts/verify_run_bot.sh
```

Windows 的 Git Bash 没有 Linux 的进程模型，需要用 bash 替身代替 Python 进程：`VERIFY_BASH_STUB=1 bash scripts/verify_run_bot.sh`。CI 在 ubuntu 上直接运行。

## 静态检查

用 ruff 做静态检查，配置在仓库根的 `ruff.toml`：

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff check . --fix
```

当前只启用 `E4` / `E7` / `E9` / `F` 四组规则，盯的是真问题——未使用的 import、
未定义的名字、语法错误——而不是代码风格。`ruff.toml` 里注释掉了 `I`（import 排序）、
`W`（空白）、`UP`（语法现代化）、`BLE`（裸 except）、`LOG`（logging 用法）几组，
想扩展时打开一组、修一组，别一次全开。

`ruff check` 报出的问题应该清零后再提交；确实需要保留的写 `per-file-ignores`，
不要用零散的 `# noqa`。

## 类型检查

用 mypy 做增量类型检查，只检查 `pyproject.toml` 里 `[tool.mypy]` 的 `files` 列出的模块：

```powershell
.\.venv\Scripts\python.exe -m mypy
```

没有列入的模块只提供类型信息，不报告它们自己的错误（`follow_imports = "silent"`），所以存量代码不需要为此改动。列入的模块按 `disallow_untyped_defs` 检查：函数签名必须完整标注。

新增或抽取出来的稳定边界（例如余额服务、repository、调度器的状态机）在同一个提交里加入检查：

1. 给模块补齐类型标注，让 `mypy` 本地通过。
2. 把模块路径加进 `pyproject.toml` 的 `[tool.mypy] files`。
3. 缺少类型存根的第三方库，在 `[[tool.mypy.overrides]]` 里为该库设置 `ignore_missing_imports = true`，不要在模块里写整文件的 `# type: ignore`。

`warn_unused_ignores` 已开启，修好问题后残留的 `# type: ignore` 会报错，需要删除。

## CI

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) 在每个 pull request 和推送到 `main` 时运行：

- `lint`：`uv lock --check`（锁与 `pyproject.toml` 一致）、导出的 `requirements*.txt` 与锁一致、ruff、mypy。
- `test`：除 `tests/integration` 以外的 pytest。
- `integration`：`mysql:8.4` service 上的 `tests/integration`；目录里还没有测试时不算失败，但全部被跳过会失败。
- `image`：构建镜像并运行启动冒烟检查，确认日志同时进入 stdout 和挂载的日志目录。
- `run-bot-script`：运行 `scripts/verify_run_bot.sh`。

本地想复现某一项时，运行上面各节对应的命令即可。
