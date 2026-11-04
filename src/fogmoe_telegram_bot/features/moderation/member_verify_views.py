"""入群验证的展示：欢迎消息与九宫格的文案、按钮与回调数据、弹框里的提示。

只依赖 Telegram 的按钮类型，不发消息；按钮的处理在 `member_verify.py`，出题与判题在 `operations/verify_game.py`。
"""

from __future__ import annotations

from dataclasses import dataclass

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from fogmoe_telegram_bot.features.moderation import admin_ban_views
from fogmoe_telegram_bot.features.moderation.operations.member_verify import VERIFY_SECONDS
from fogmoe_telegram_bot.features.moderation.operations.verify_game import (
    MAX_MISTAKES,
    ROUNDS_TO_PASS,
    Game,
    Outcome,
)

# 回调数据前缀；注册的 pattern 是 `^verify_`
CALLBACK_PREFIX = "verify"
GRID_COLUMNS = 3
START_LABEL = "🎮 开始验证"
HINT_LABEL = "💡 查看提示"

NOT_YOURS = "这不是为您准备的验证按钮。"
STALE = "验证已失效或已处理。"
PASSED_ALERT = "验证成功！"
PASSED_TEXT = "验证通过，欢迎加入群组！"
FAILED_ALERT = f"答错 {MAX_MISTAKES} 次，验证失败。"
LEFT_TEXT = "新成员在验证前离开了群组。"
UNMUTE_FAILED = "验证时出现错误，请稍后再试。"


# ---------------------------------------------------------------------------
# 回调数据与按钮
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VerifyButton:
    kind: str  # "start"、"hint" 或 "pick"
    question: int = 0  # pick：第几题
    cell: int = 0  # pick：第几格


def start_data(user_id: int) -> str:
    return f"{CALLBACK_PREFIX}_{user_id}"


def hint_data(user_id: int) -> str:
    return f"{CALLBACK_PREFIX}_{user_id}_hint"


def pick_data(user_id: int, question: int, cell: int) -> str:
    return f"{CALLBACK_PREFIX}_{user_id}_pick_{question}_{cell}"


def parse_button(data: str | None, user_id: int) -> VerifyButton | None:
    """属于 `user_id` 的验证按钮；别人的按钮或看不懂的回调数据返回 None。

    旧版本发出的 verify_<用户>_<令牌> 当作开始验证。
    """
    parts = (data or "").split("_")
    if len(parts) < 2 or parts[0] != CALLBACK_PREFIX or parts[1] != str(user_id):
        return None
    rest = parts[2:]
    if rest == ["hint"]:
        return VerifyButton("hint")
    if len(rest) == 3 and rest[0] == "pick" and rest[1].isdigit() and rest[2].isdigit():
        return VerifyButton("pick", question=int(rest[1]), cell=int(rest[2]))
    if len(rest) <= 1:
        return VerifyButton("start")
    return None


def welcome_keyboard(chat_id: int, user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(START_LABEL, callback_data=start_data(user_id)),
            admin_ban_views.ban_button(chat_id, user_id),
        ]
    ])


def game_keyboard(chat_id: int, user_id: int, game: Game) -> InlineKeyboardMarkup:
    """九宫格，下面一行是查看提示与管理员封禁。"""
    cells = [
        InlineKeyboardButton(icon, callback_data=pick_data(user_id, game.question, cell))
        for cell, icon in enumerate(game.icons)
    ]
    rows = [cells[start:start + GRID_COLUMNS] for start in range(0, len(cells), GRID_COLUMNS)]
    rows.append([
        InlineKeyboardButton(HINT_LABEL, callback_data=hint_data(user_id)),
        admin_ban_views.ban_button(chat_id, user_id),
    ])
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# 群消息（HTML）与弹框
# ---------------------------------------------------------------------------


def _greeting(user_id: int) -> str:
    """不显示名字（广告号的名字本身就是广告）。"""
    return f"欢迎 {admin_ban_views.neutral_mention(user_id, '新成员')} 加入群组！"


def welcome_text(user_id: int) -> str:
    return (
        f"{_greeting(user_id)}请在 {VERIFY_SECONDS // 60} 分钟内点【{START_LABEL}】，"
        "玩一把小游戏完成验证。"
    )


def game_text(user_id: int, game: Game) -> str:
    """群里看到的九宫格说明：不写目标，目标只在弹框里。"""
    mistakes = f"（已答错 {game.mistakes} 次）" if game.mistakes else ""
    return (
        f"{_greeting(user_id)}\n"
        f"第 {game.streak + 1}/{ROUNDS_TO_PASS} 题：在下面的九宫格里点出弹框提示的图标。\n"
        f"忘了是哪个就点【{HINT_LABEL}】；答错 {MAX_MISTAKES} 次会被移出群组{mistakes}。"
    )


def _removed(reason: str, user_id: int) -> str:
    """没通过验证、已经移出群组：欢迎消息换成这句。被移出的人看不到它，所以不写「您」。"""
    return f"{reason}，{admin_ban_views.neutral_mention(user_id, '新成员')} 已被移出群组。"


def failed_text(user_id: int) -> str:
    return _removed("验证失败", user_id)


def timeout_text(user_id: int) -> str:
    return _removed("验证超时", user_id)


def prompt(game: Game, outcome: Outcome | None = None) -> str:
    """弹框里的题目，只有本人看得到；刚点完格子时前面先说答对、答错还是题目已经换了。"""
    if outcome is Outcome.NEXT:
        lead = "答对了！\n"
    elif outcome is Outcome.WRONG:
        lead = f"点错了，再错 {MAX_MISTAKES - game.mistakes} 次就会被移出群组，换一题重新来。\n"
    elif outcome is Outcome.STALE:
        lead = "题目已经换了。\n"
    else:
        lead = ""
    return f"{lead}第 {game.streak + 1}/{ROUNDS_TO_PASS} 题：请在九宫格里点 {game.target_icon}"
