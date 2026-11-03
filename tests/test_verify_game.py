import random

from fogmoe_telegram_bot.features.moderation.operations.verify_game import (
    GRID_SIZE,
    KEEP_SECONDS,
    MAX_MISTAKES,
    ROUNDS_TO_PASS,
    Outcome,
    VerifyGames,
)

CHAT_ID = -100
USER_ID = 42
MESSAGE_ID = 2


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _games(clock=None):
    return VerifyGames(clock=clock or _Clock(), rng=random.Random(0))


def _pick(games, game, *, correct, question=None):
    cell = game.target if correct else (game.target + 1) % GRID_SIZE
    return games.pick(CHAT_ID, USER_ID, MESSAGE_ID, game.question if question is None else question, cell)


def test_a_question_is_nine_distinct_icons_with_the_target_among_them():
    game = _games().start(CHAT_ID, USER_ID, MESSAGE_ID)

    assert len(game.icons) == GRID_SIZE == len(set(game.icons))
    assert game.target_icon in game.icons


def test_passing_takes_consecutive_correct_answers():
    games = _games()
    game = games.start(CHAT_ID, USER_ID, MESSAGE_ID)

    outcomes = [_pick(games, game, correct=True) for _ in range(ROUNDS_TO_PASS)]

    assert outcomes == [Outcome.NEXT] * (ROUNDS_TO_PASS - 1) + [Outcome.PASSED]
    assert game.passed


def test_a_wrong_answer_resets_the_streak_and_changes_the_question():
    games = _games()
    game = games.start(CHAT_ID, USER_ID, MESSAGE_ID)
    _pick(games, game, correct=True)
    question = game.question

    assert _pick(games, game, correct=False) is Outcome.WRONG
    assert game.streak == 0 and game.mistakes == 1 and game.question == question + 1


def test_running_out_of_mistakes_fails():
    games = _games()
    game = games.start(CHAT_ID, USER_ID, MESSAGE_ID)

    outcomes = [_pick(games, game, correct=False) for _ in range(MAX_MISTAKES)]

    assert outcomes == [Outcome.WRONG] * (MAX_MISTAKES - 1) + [Outcome.FAILED]
    assert _pick(games, game, correct=True) is Outcome.FAILED


def test_a_button_from_an_old_question_counts_for_nothing():
    games = _games()
    game = games.start(CHAT_ID, USER_ID, MESSAGE_ID)
    _pick(games, game, correct=True)

    assert _pick(games, game, correct=False, question=game.question - 1) is Outcome.STALE
    assert game.streak == 1 and game.mistakes == 0


def test_starting_again_keeps_the_mistakes():
    games = _games()
    game = games.start(CHAT_ID, USER_ID, MESSAGE_ID)
    _pick(games, game, correct=False)

    assert games.start(CHAT_ID, USER_ID, MESSAGE_ID) is game
    assert game.mistakes == 1


def test_a_game_belongs_to_its_welcome_message():
    games = _games()
    games.start(CHAT_ID, USER_ID, MESSAGE_ID)

    assert games.get(CHAT_ID, USER_ID, MESSAGE_ID + 1) is None
    assert games.pick(CHAT_ID, USER_ID, MESSAGE_ID + 1, 1, 0) is None
    games.drop(CHAT_ID, USER_ID, MESSAGE_ID + 1)
    assert games.get(CHAT_ID, USER_ID, MESSAGE_ID) is not None
    games.drop(CHAT_ID, USER_ID, MESSAGE_ID)
    assert games.get(CHAT_ID, USER_ID, MESSAGE_ID) is None


def test_old_games_are_forgotten():
    clock = _Clock()
    games = _games(clock)
    games.start(CHAT_ID, USER_ID, MESSAGE_ID)

    clock.now += KEEP_SECONDS

    assert games.get(CHAT_ID, USER_ID, MESSAGE_ID) is None
