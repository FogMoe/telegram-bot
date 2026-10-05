import os
import sys
from contextlib import ExitStack
from pathlib import Path

import pytest


MODULES_DIR = Path(__file__).resolve().parents[1] / "modules"
if str(MODULES_DIR) not in sys.path:
    sys.path.insert(0, str(MODULES_DIR))


# 测试默认不读开发者的 .env（BOT_ENV_FILE 为空即不读 env 文件）；只有显式开启真实连通性
# 检查时才保留默认行为，让 tests/test_env_api_connectivity.py 拿到 .env 里的真实配置。
if os.environ.get("RUN_ENV_API_CONNECTIVITY_TESTS", "").strip().lower() not in {
    "1",
    "true",
    "yes",
    "on",
}:
    os.environ.setdefault("BOT_ENV_FILE", "")
    _HERMETIC_SETTINGS = True
else:
    _HERMETIC_SETTINGS = False


# 基线配置只有代码默认值：开发者 shell 里的环境变量也不会影响测试。
# 单个测试要改配置，用下面的 `settings_override` 夹具（或 monkeypatch 个别 `config.<NAME>`）。
from core import config  # noqa: E402

if _HERMETIC_SETTINGS:
    config.install_settings(config.AppSettings.from_values())

# 组装层在启动时注入历史回调，测试沿用同一份装配，避免测到未装配的降级路径。
from features.conversation.history_hooks import install_history_hooks  # noqa: E402

install_history_hooks()

_INTEGRATION_DIR = Path(__file__).resolve().parent / "integration"


def pytest_addoption(parser):
    parser.addoption(
        "--run-slow",
        action="store_true",
        help="同时运行标记为 slow 的测试（CI 带这个参数）",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "slow: 单个超过约 0.1 秒的测试（真实等待、扫描源码、组装 Application），本地默认跳过"
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-slow"):
        return
    skip = pytest.mark.skip(reason="slow：本地默认跳过，加 --run-slow 运行")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _no_database_in_unit_tests(request, monkeypatch):
    """单元测试不连数据库：没打桩就走到 `core.db` 的访问立即按连接失败处理。

    否则会真的去连配置里的默认地址，在 Windows 上连接被拒要等约 4 秒才失败。
    tests/integration 用 TEST_MYSQL_URL 指向的真实库，不受影响。
    """
    if _INTEGRATION_DIR in request.node.path.parents:
        return
    from sqlalchemy.exc import OperationalError

    from core import db

    def refuse_connection():
        raise OperationalError("单元测试不连接数据库", None, ConnectionRefusedError())

    monkeypatch.setattr(db, "get_engine", refuse_connection)


@pytest.fixture
def settings_override():
    """`settings_override(NAME=value, ...)`：本测试内使用「代码默认值 + 传入值」的配置。

    不读 `.env` 与环境变量；所有在调用时读 `config.<NAME>` 的代码（provider 解析、对话编排等）
    都能看到。再次调用会整体替换上一次的配置，返回生效的设置对象。
    结束时无条件恢复进入测试时的配置，被测代码自己 `install_settings`（例如 `create_application`）
    也不会漏到别的测试。先用它装配置，再 monkeypatch 个别常量：安装配置会重写全部模块级常量。
    """
    previous = config.current_settings()
    try:
        with ExitStack() as stack:

            def apply(**values):
                return stack.enter_context(config.override_settings(**values))

            yield apply
    finally:
        config.install_settings(previous)
