"""scripts/ci_mysql_changes.py：CI 只在改动涉及 MySQL 时跑 tests/integration。"""

import functools
import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = REPO_ROOT / "src" / "fogmoe_telegram_bot"
MODULE = "src/fogmoe_telegram_bot/features/example.py"


@functools.cache
def _detector():
    path = REPO_ROOT / "scripts" / "ci_mysql_changes.py"
    spec = importlib.util.spec_from_file_location("ci_mysql_changes", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("path", "source"),
    [
        (MODULE, "from sqlalchemy.ext.asyncio import AsyncConnection\n"),
        (MODULE, "from fogmoe_telegram_bot.core import balance, sql\n"),
        (MODULE, "from fogmoe_telegram_bot.core import (\n    config,\n    db,\n)\n"),
        (MODULE, "from ..core import sql\n"),
        (MODULE, "def later():\n    import asyncmy\n"),
        ("src/fogmoe_telegram_bot/core/balance.py", "from .sql import fetch_one\n"),
        ("src/fogmoe_telegram_bot/core/__init__.py", "from . import db\n"),
        # 解析不了就按涉及处理
        (MODULE, "def broken(:\n"),
    ],
)
def test_db_imports_count_as_mysql(path, source):
    assert _detector().touches_db(source, path)


@pytest.mark.parametrize(
    "source",
    [
        "from fogmoe_telegram_bot.core import config, i18n\n",
        "from fogmoe_telegram_bot.core.sql_style import x\n",
        # 相对 import 按所在包换算：这是 features.sql，不是 core.sql
        "from .sql import fetch_one\n",
        '"""这里只在文档里提到 sqlalchemy。"""\n',
    ],
)
def test_other_imports_do_not_count(source):
    assert not _detector().touches_db(source, MODULE)


def test_only_mysql_related_paths_are_reported():
    sources = {
        "src/fogmoe_telegram_bot/features/ai/tool_runner.py": ["import asyncio\n"],
        # 改动后去掉了数据库访问，改动前的版本仍算
        "src/fogmoe_telegram_bot/features/games/dice.py": [
            "from fogmoe_telegram_bot.core import sql\n",
            "x = 1\n",
        ],
    }
    paths = [
        "docs/architecture.md",
        "tests/test_tool_runner.py",
        "src/fogmoe_telegram_bot/resources/telegram_help.md",
        *sources,
        "alembic/versions/0025_example.py",
        "tests/integration/test_stake_flow.py",
        "uv.lock",
    ]

    reasons = _detector().mysql_reasons(paths, lambda path: sources.get(path, []))

    assert [reason.split("（")[0] for reason in reasons] == [
        "src/fogmoe_telegram_bot/features/games/dice.py",
        "alembic/versions/0025_example.py",
        "tests/integration/test_stake_flow.py",
        "uv.lock",
    ]


def test_current_db_layer_is_detected():
    """判断规则跟着代码走：数据库层和各 repositories 模块按现在的源码都算涉及 MySQL。"""
    repositories = PACKAGE_DIR.glob("features/**/repositories/*.py")
    modules = {
        path.relative_to(REPO_ROOT).as_posix(): path.read_text(encoding="utf-8")
        for path in [PACKAGE_DIR / "core" / "db.py", PACKAGE_DIR / "core" / "sql.py", *repositories]
        if path.name != "__init__.py"
    }
    missed = [path for path, source in modules.items() if not _detector().touches_db(source, path)]
    assert len(modules) > 10
    assert missed == []
