"""scripts/ci_mysql_changes.py：CI 只在改动涉及 MySQL 时跑 tests/integration。"""

import ast
import functools
import importlib.util
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = REPO_ROOT / "src" / "fogmoe_telegram_bot"
MODULE = "src/fogmoe_telegram_bot/features/example.py"
# 字符串常量开头是一条 SQL 语句；f-string 和相邻字面量拼接的第一段也算。
_SQL_STATEMENT_RE = re.compile(
    r"^\s*(SELECT|INSERT\s+INTO|UPDATE\s+\S+\s+SET|DELETE\s+FROM|REPLACE\s+INTO)\s", re.I
)


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
        (MODULE, "from fogmoe_telegram_bot.core import mysql_connection\n"),
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
        "src/fogmoe_telegram_bot/core/config.py",
        "uv.lock",
    ]

    reasons = _detector().mysql_reasons(paths, lambda path: sources.get(path, []))

    assert [reason.split("（")[0] for reason in reasons] == [
        "src/fogmoe_telegram_bot/features/games/dice.py",
        "alembic/versions/0025_example.py",
        "tests/integration/test_stake_flow.py",
        "src/fogmoe_telegram_bot/core/config.py",
        "uv.lock",
    ]


def _has_sql_statement(source: str) -> bool:
    return any(
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and _SQL_STATEMENT_RE.match(node.value)
        for node in ast.walk(ast.parse(source))
    )


@pytest.mark.slow
def test_current_db_access_is_detected():
    """判断规则跟着代码走：写了 SQL 语句的模块和各 repositories 模块按现在的源码都算涉及 MySQL。

    新加一层数据库封装时这里会失败，提醒把它加进 `DB_MODULES`。
    """
    sources = {
        path.relative_to(REPO_ROOT).as_posix(): path.read_text(encoding="utf-8")
        for path in PACKAGE_DIR.rglob("*.py")
    }
    modules = {
        path: source
        for path, source in sources.items()
        if _has_sql_statement(source)
        or ("/repositories/" in path and not path.endswith("__init__.py"))
    }
    missed = [path for path, source in modules.items() if not _detector().touches_db(source, path)]
    assert "src/fogmoe_telegram_bot/features/ai/schedule_limits.py" in modules
    assert len(modules) > 30
    assert missed == []
