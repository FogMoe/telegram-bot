"""金币只通过余额服务变动：旧的余额函数已经删除，游戏与经济模块也不直接改 user 表的余额。"""

import re
from pathlib import Path

import pytest

PACKAGE_DIR = Path(__file__).resolve().parents[1] / "src" / "fogmoe_telegram_bot"
GAMES_DIR = PACKAGE_DIR / "features" / "games"
ECONOMY_DIR = PACKAGE_DIR / "features" / "economy"

# 已删除的旧接口（process_user 的金币函数与奖池的 legacy 委托）：模块里既不能再定义，也不能再调用。
REMOVED_BALANCE_FUNCTIONS = (
    "add_free_coins",
    "add_paid_coins",
    "spend_user_coins",
    "update_user_coins",
    "async_update_user_coins",
    "add_to_pool",
    "subtract_from_pool",
)
DIRECT_BALANCE_WRITE = re.compile(r"UPDATE\s+`?user`?\s+SET[^;\"']*\bcoins", re.IGNORECASE)


def sources(directory: Path):
    return {path: path.read_text(encoding="utf-8") for path in directory.rglob("*.py")}


@pytest.mark.slow
def test_the_removed_balance_functions_are_neither_defined_nor_called():
    offenders = {
        str(path.relative_to(PACKAGE_DIR)): [
            name for name in REMOVED_BALANCE_FUNCTIONS if re.search(rf"\b{name}\b", text)
        ]
        for path, text in sources(PACKAGE_DIR).items()
    }

    assert {path: names for path, names in offenders.items() if names} == {}


def test_games_and_economy_do_not_write_user_balances_directly():
    offenders = [
        str(path.relative_to(PACKAGE_DIR))
        for directory in (GAMES_DIR, ECONOMY_DIR)
        for path, text in sources(directory).items()
        if DIRECT_BALANCE_WRITE.search(text)
    ]

    assert offenders == []
