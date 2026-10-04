"""游戏模块只通过余额服务变动金币：不调用 process_user 里待移除的旧接口，也不直接改 user 表的余额。"""

import re
from pathlib import Path

GAMES_DIR = Path(__file__).resolve().parents[1] / "modules" / "features" / "games"

LEGACY_BALANCE_CALLS = (
    "add_free_coins",
    "add_paid_coins",
    "spend_user_coins",
    "update_user_coins",
    "async_update_user_coins",
)
DIRECT_BALANCE_WRITE = re.compile(r"UPDATE\s+`?user`?\s+SET[^;\"']*\bcoins", re.IGNORECASE)


def games_sources():
    return {path: path.read_text(encoding="utf-8") for path in GAMES_DIR.rglob("*.py")}


def test_games_do_not_call_the_legacy_balance_functions():
    offenders = {
        str(path.relative_to(GAMES_DIR)): [name for name in LEGACY_BALANCE_CALLS if name in text]
        for path, text in games_sources().items()
    }

    assert {path: names for path, names in offenders.items() if names} == {}


def test_games_do_not_write_user_balances_directly():
    offenders = [
        str(path.relative_to(GAMES_DIR))
        for path, text in games_sources().items()
        if DIRECT_BALANCE_WRITE.search(text)
    ]

    assert offenders == []
