"""判断一次改动是否涉及 MySQL，CI 据此决定要不要跑 `tests/integration`。

    python3 scripts/ci_mysql_changes.py <base> [<head>]   # stdout 打印 true / false，理由写到 stderr

算涉及 MySQL 的改动：

- 固定路径：迁移（`alembic/`、`alembic.ini`）、集成测试（`tests/integration/` 和对它同样生效的
  `tests/conftest.py`）、依赖与 pytest 配置（`pyproject.toml`、`uv.lock`）、CI 本身（`ci.yml` 与本脚本）；
- `src/` 下改动前或改动后 import 了 sqlalchemy、asyncmy、alembic 或项目 `core.db`、`core.sql` 的模块。

只用标准库，CI 用 runner 自带的 python3 运行，不需要 `uv sync`。拿不准时（解析不了的文件）算涉及；
git 比较失败时退出码非 0，由 CI 按涉及处理。
"""

from __future__ import annotations

import ast
import subprocess
import sys
from collections.abc import Callable, Iterable

ALWAYS_PATHS = (
    "alembic/",
    "alembic.ini",
    "tests/integration/",
    "tests/conftest.py",
    "pyproject.toml",
    "uv.lock",
    ".github/workflows/ci.yml",
    "scripts/ci_mysql_changes.py",
)
SOURCE_ROOT = "src/"
DB_MODULES = (
    "sqlalchemy",
    "asyncmy",
    "alembic",
    "fogmoe_telegram_bot.core.db",
    "fogmoe_telegram_bot.core.sql",
)


def imported_modules(source: str, path: str) -> set[str]:
    """源码里所有 import 的完整模块名，函数内的延迟 import 也算。

    `from 包 import 名字` 同时记 `包` 和 `包.名字`，因为名字可能是子模块；相对 import 按 `path` 换算成绝对名。
    """
    # 普通模块和 __init__.py 的相对 import 都以文件所在目录为起点。
    package = path.removeprefix(SOURCE_ROOT).split("/")[:-1]
    names: set[str] = set()
    for node in ast.walk(ast.parse(source, filename=path)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base_parts = package[: len(package) - node.level + 1]
                base = ".".join([*base_parts, node.module] if node.module else base_parts)
            else:
                base = node.module or ""
            names.add(base)
            names.update(f"{base}.{alias.name}" for alias in node.names)
    return names


def touches_db(source: str, path: str) -> bool:
    try:
        names = imported_modules(source, path)
    except SyntaxError:
        return True
    return any(name == db or name.startswith(f"{db}.") for name in names for db in DB_MODULES)


def mysql_reasons(paths: Iterable[str], versions: Callable[[str], Iterable[str]]) -> list[str]:
    """改动里涉及 MySQL 的文件及原因；`versions(path)` 给出该文件改动前后仍存在的源码。"""
    reasons: list[str] = []
    for path in paths:
        if path.startswith(ALWAYS_PATHS):
            reasons.append(path)
        elif path.startswith(SOURCE_ROOT) and path.endswith(".py"):
            if any(touches_db(source, path) for source in versions(path)):
                reasons.append(f"{path}（import 了数据库模块）")
    return reasons


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8")


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print(__doc__, file=sys.stderr)
        return 2
    base, head = argv[1], argv[2] if len(argv) == 3 else "HEAD"
    diff = _git("diff", "--name-only", "--no-renames", base, head)
    if diff.returncode:
        print(f"无法比较 {base} 与 {head}：{diff.stderr.strip()}", file=sys.stderr)
        return 1

    def versions(path: str) -> list[str]:
        shown = (_git("show", f"{rev}:{path}") for rev in (base, head))
        return [result.stdout for result in shown if result.returncode == 0]

    reasons = mysql_reasons(diff.stdout.splitlines(), versions)
    for reason in reasons:
        print(f"涉及 MySQL：{reason}", file=sys.stderr)
    if not reasons:
        print("没有涉及 MySQL 的改动", file=sys.stderr)
    print("true" if reasons else "false")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
