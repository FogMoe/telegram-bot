"""入群验证的小游戏：在九宫格里点出弹框提示的图标，连续答对 `ROUNDS_TO_PASS` 题通过，累计答错 `MAX_MISTAKES` 次失败。

不依赖 Telegram，也不碰数据库。规则：

- 目标只写在只有本人看得到的弹框里，群消息里只有九宫格，按钮的回调数据只带题号和格子编号。
- 每道题都重新抽图标、重新排列；答错时连续答对的次数清零，换一道新题。
- 题号随换题递增，按钮带着题号：消息没能及时换成新题时，点旧题的格子不算对也不算错。
- 一局以欢迎消息为身份（同 `member_verify.py` 的一轮验证），只放在进程内存里，保留 `KEEP_SECONDS`；
  重启后丢失，成员再点按钮时重新开一局，答错次数从零算起。通过与否最终仍以认领验证记录为准。
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from .member_verify import VERIFY_SECONDS

# 彼此一眼能分清的图标，每道题从中抽 GRID_SIZE 个
ICONS = (
    "🐶", "🐱", "🐼", "🐨", "🦁", "🐮", "🐷", "🐸", "🐵", "🐰",
    "🦊", "🐧", "🐔", "🦄", "🐙", "🐢", "🐳", "🦋", "🐝", "🍎",
    "🍌", "🍇", "🍉", "🍓", "🥕", "🌽", "🚗", "🎈", "🎁", "🔑",
)
GRID_SIZE = 9
ROUNDS_TO_PASS = 2
MAX_MISTAKES = 3
# 比一轮验证的时限长，验证超时之前进行中的局不会被清掉
KEEP_SECONDS = VERIFY_SECONDS * 2
# 局数超过这个数时先清掉过期的，还是太多就整体清空
MAX_ENTRIES = 10_000


class Outcome(Enum):
    NEXT = "next"  # 答对了，换下一题
    PASSED = "passed"  # 答对了最后一题；已经通过、只是解除禁言失败时再点也是它
    WRONG = "wrong"  # 答错了，换一道新题
    FAILED = "failed"  # 答错次数用完
    STALE = "stale"  # 点的是旧题的格子，不算


@dataclass(slots=True)
class Game:
    message_id: int
    started: float
    icons: tuple[str, ...] = ()
    target: int = 0
    question: int = 0  # 换题就加一，按钮带着它
    streak: int = 0  # 连续答对的题数
    mistakes: int = 0
    passed: bool = False

    @property
    def target_icon(self) -> str:
        return self.icons[self.target]


class VerifyGames:
    """每个（群, 用户）进行中的一局。"""

    def __init__(
        self, clock: Callable[[], float] = time.monotonic, rng: random.Random | None = None
    ) -> None:
        self._clock = clock
        self._rng = rng or random.SystemRandom()
        self._games: dict[tuple[int, int], Game] = {}

    def get(self, chat_id: int, user_id: int, message_id: int | None) -> Game | None:
        """这条欢迎消息上进行中的一局；没开始、已经换成别的消息或已经过期时返回 None。"""
        game = self._games.get((chat_id, user_id))
        if game is None or game.message_id != message_id or self._clock() - game.started >= KEEP_SECONDS:
            return None
        return game

    def start(self, chat_id: int, user_id: int, message_id: int) -> Game:
        """在这条欢迎消息上开一局；已经开了的原样返回，不重置答错次数。"""
        game = self.get(chat_id, user_id, message_id)
        if game is not None:
            return game
        now = self._clock()
        if len(self._games) >= MAX_ENTRIES:
            self._games = {key: item for key, item in self._games.items() if now - item.started < KEEP_SECONDS}
            if len(self._games) >= MAX_ENTRIES:
                self._games.clear()
        game = Game(message_id, now)
        self._new_question(game)
        self._games[(chat_id, user_id)] = game
        return game

    def pick(self, chat_id: int, user_id: int, message_id: int | None, question: int, cell: int) -> Outcome | None:
        """点了第 `question` 题的第 `cell` 格；这条消息上没有进行中的局时返回 None。"""
        game = self.get(chat_id, user_id, message_id)
        if game is None:
            return None
        if game.passed:
            return Outcome.PASSED
        if game.mistakes >= MAX_MISTAKES:
            return Outcome.FAILED
        if question != game.question:
            return Outcome.STALE
        if cell == game.target:
            game.streak += 1
            if game.streak >= ROUNDS_TO_PASS:
                game.passed = True
                return Outcome.PASSED
            self._new_question(game)
            return Outcome.NEXT
        game.streak = 0
        game.mistakes += 1
        if game.mistakes >= MAX_MISTAKES:
            return Outcome.FAILED
        self._new_question(game)
        return Outcome.WRONG

    def drop(self, chat_id: int, user_id: int, message_id: int | None) -> None:
        """这一轮验证结束了：忘掉这条欢迎消息上的局（别的消息上的局不动）。"""
        game = self._games.get((chat_id, user_id))
        if game is not None and game.message_id == message_id:
            del self._games[(chat_id, user_id)]

    def _new_question(self, game: Game) -> None:
        game.icons = tuple(self._rng.sample(ICONS, GRID_SIZE))
        game.target = self._rng.randrange(GRID_SIZE)
        game.question += 1


GAMES = VerifyGames()
