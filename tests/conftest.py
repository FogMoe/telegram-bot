import os
import sys
from pathlib import Path


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


# 组装层在启动时注入历史回调，测试沿用同一份装配，避免测到未装配的降级路径。
from features.conversation.history_hooks import install_history_hooks  # noqa: E402

install_history_hooks()
