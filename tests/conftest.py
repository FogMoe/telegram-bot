import sys
from pathlib import Path


MODULES_DIR = Path(__file__).resolve().parents[1] / "modules"
if str(MODULES_DIR) not in sys.path:
    sys.path.insert(0, str(MODULES_DIR))


# 组装层在启动时注入历史回调，测试沿用同一份装配，避免测到未装配的降级路径。
from features.conversation.history_hooks import install_history_hooks  # noqa: E402

install_history_hooks()
