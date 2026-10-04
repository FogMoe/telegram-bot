import os
import subprocess
import sys

import pytest

from core import config

ENV_FILE_VAR = config.ENV_FILE_VAR


def test_env_file_defaults_to_repo_dotenv():
    assert config.resolve_env_file({}) == config.BASE_DIR / ".env"


def test_blank_override_disables_env_file():
    assert config.resolve_env_file({ENV_FILE_VAR: ""}) is None
    assert config.resolve_env_file({ENV_FILE_VAR: "   "}) is None


def test_override_points_to_custom_file(tmp_path):
    custom = tmp_path / "custom.env"
    assert config.resolve_env_file({ENV_FILE_VAR: str(custom)}) == custom


def test_pytest_session_does_not_load_dotenv():
    if os.environ.get("RUN_ENV_API_CONNECTIVITY_TESTS"):
        pytest.skip("connectivity check intentionally reads the real .env")
    assert config.AppSettings.model_config["env_file"] is None


def _read_admin_user_id(env: dict[str, str]) -> str:
    code = "from core import config; print(config.ADMIN_USER_ID)"
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        cwd=config.BASE_DIR / "modules",
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def test_import_reads_only_the_selected_env_file(tmp_path):
    env_file = tmp_path / "custom.env"
    env_file.write_text("ADMIN_USER_ID=424242\n", encoding="utf-8")
    base_env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"ADMIN_USER_ID", ENV_FILE_VAR}
    }

    assert _read_admin_user_id({**base_env, ENV_FILE_VAR: str(env_file)}) == "424242"
    assert _read_admin_user_id({**base_env, ENV_FILE_VAR: ""}) != "424242"
