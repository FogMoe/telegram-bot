"""经济与游戏的分层边界：SQL 只在 repository 里，repository 不持有事务也不含业务，操作与适配层不碰 SQL。

见 docs/architecture.md 的「经济与游戏的分层」。检查的是代码结构（AST），不运行任何业务。
"""

import ast
import re
from pathlib import Path

import pytest

PACKAGE_DIR = Path(__file__).resolve().parents[1] / "src" / "fogmoe_telegram_bot"
ECONOMY_DIR = PACKAGE_DIR / "features" / "economy"
GAMES_DIR = PACKAGE_DIR / "features" / "games"
CRYPTO_DIR = PACKAGE_DIR / "features" / "crypto"
XFEED_DIR = PACKAGE_DIR / "features" / "xfeed"
SPAM_AI_DIR = PACKAGE_DIR / "features" / "moderation" / "spam_ai"
# crypto 里只有这两个入口持有金币，SQL 已收拢到 crypto/repositories；其余模块（图表设置等）不在范围内。
CRYPTO_COVERED = ("crypto_predict.py", "swap_fogmoe_solana_token.py")

SQL_STATEMENT = re.compile(
    r"\b(SELECT\b.+\bFROM|INSERT\s+INTO|UPDATE\s+\S+\s+SET|DELETE\s+FROM)\b",
    re.IGNORECASE | re.DOTALL,
)
# 直接执行 SQL 的调用：只有 repository 可以用。
SQL_CALLS = {"exec_driver_sql", "fetch_one", "fetch_all", "execute"}
# repository 不持有事务：事务由业务操作开，repository 只接受 connection。
TRANSACTION_CALLS = {"transaction", "commit", "rollback", "begin", "run_in_transaction"}
FORBIDDEN_REPOSITORY_IMPORTS = (
    "telegram",
    "fogmoe_telegram_bot.core.balance",
    "fogmoe_telegram_bot.core.stake_reward_pool",
)


def python_files(directory: Path):
    return sorted(path for path in directory.rglob("*.py") if "__pycache__" not in path.parts)


def non_repository_files(directory: Path):
    return [path for path in python_files(directory) if "repositories" not in path.parts]


def repository_files():
    return [
        path
        for directory in (ECONOMY_DIR, GAMES_DIR, CRYPTO_DIR, XFEED_DIR, SPAM_AI_DIR)
        for path in python_files(directory / "repositories")
        if path.name != "__init__.py"
    ]


def string_constants(tree: ast.Module):
    """代码里的字符串常量（不含各级 docstring）。"""
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                yield node.value


def called_names(tree: ast.Module):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                yield func.attr
            elif isinstance(func, ast.Name):
                yield func.id


def imported_modules(tree: ast.Module):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            base = ("." * node.level) + (node.module or "")
            yield base
            for alias in node.names:
                yield f"{base}.{alias.name}"


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def relative(path: Path) -> str:
    return path.relative_to(PACKAGE_DIR).as_posix()


@pytest.mark.parametrize(
    "directory",
    [ECONOMY_DIR, GAMES_DIR, XFEED_DIR, SPAM_AI_DIR],
    ids=["economy", "games", "xfeed", "spam_ai"],
)
def test_handlers_and_operations_contain_no_sql(directory):
    offenders = {}
    for path in non_repository_files(directory):
        tree = parse(path)
        statements = [text for text in string_constants(tree) if SQL_STATEMENT.search(text)]
        calls = sorted(set(called_names(tree)) & SQL_CALLS)
        if statements or calls:
            offenders[relative(path)] = {"sql": statements[:1], "calls": calls}

    assert offenders == {}


@pytest.mark.slow
def test_the_crypto_entry_points_that_hold_coins_contain_no_sql():
    offenders = {}
    for name in CRYPTO_COVERED:
        tree = parse(CRYPTO_DIR / name)
        statements = [text for text in string_constants(tree) if SQL_STATEMENT.search(text)]
        calls = sorted(set(called_names(tree)) & SQL_CALLS)
        if statements or calls:
            offenders[name] = {"sql": statements[:1], "calls": calls}

    assert offenders == {}


def test_every_repository_is_covered_by_the_scan():
    names = {relative(path) for path in repository_files()}

    assert "features/economy/repositories/shop.py" in names
    assert "features/games/repositories/gamble.py" in names
    assert "features/crypto/repositories/predictions.py" in names
    assert "features/xfeed/repositories/feeds.py" in names
    assert "features/moderation/spam_ai/repositories/groups.py" in names
    assert len(names) >= 12


@pytest.mark.parametrize("path", repository_files(), ids=relative)
def test_repositories_do_not_own_transactions(path):
    assert set(called_names(parse(path))) & TRANSACTION_CALLS == set()


@pytest.mark.parametrize("path", repository_files(), ids=relative)
def test_repositories_do_not_import_telegram_or_the_balance_service(path):
    imports = set(imported_modules(parse(path)))

    offenders = sorted(
        name
        for name in imports
        for forbidden in FORBIDDEN_REPOSITORY_IMPORTS
        if name == forbidden or name.startswith(forbidden + ".")
    )

    assert offenders == []


def test_economy_operations_do_not_import_telegram():
    offenders = [
        relative(path)
        for path in python_files(ECONOMY_DIR / "operations")
        if any(name.split(".")[0] == "telegram" for name in imported_modules(parse(path)))
    ]

    assert offenders == []


@pytest.mark.parametrize(
    "path",
    [
        XFEED_DIR / "operations.py",
        XFEED_DIR / "source.py",
        SPAM_AI_DIR / "operations.py",
        SPAM_AI_DIR / "judge.py",
    ],
    ids=relative,
)
def test_feature_rules_and_sources_do_not_import_telegram(path):
    assert not any(
        module.split(".")[0] == "telegram" for module in imported_modules(parse(path))
    )
