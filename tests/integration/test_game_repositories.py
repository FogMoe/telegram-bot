"""游戏的 repository：单条语句级别的读写语义，以及「事务由调用方持有」的约定（真实 MySQL）。

状态转换、余额变动与恢复在 test_gamble_rounds.py、test_rps_games.py、test_game_balances.py；
这里只验证每个 repository 函数自己的行为。
"""

from datetime import date

import pytest
from economy_support import seed_user
from mysql_support import execute, fetch, fetch_scalar, run
from sqlalchemy.exc import IntegrityError

from fogmoe_telegram_bot.core import sql
from fogmoe_telegram_bot.features.games.repositories import gamble as gamble_repository
from fogmoe_telegram_bot.features.games.repositories import omikuji as omikuji_repository
from fogmoe_telegram_bot.features.games.repositories import rpg as rpg_repository
from fogmoe_telegram_bot.features.games.repositories import rps as rps_repository
from fogmoe_telegram_bot.features.games.repositories.rps import Seat


def in_transaction(work):
    """在一个事务里运行 `work(connection)` 并返回结果；抛出异常时事务回滚。"""

    async def scenario():
        async with sql.transaction() as connection:
            return await work(connection)

    return run(scenario())


def read(work):
    async def scenario():
        async with sql.connect() as connection:
            return await work(connection)

    return run(scenario())


class TestGambleRepository:
    def open_round(self, chat_id=-100, seconds=300):
        return in_transaction(lambda c: gamble_repository.insert_round(c, chat_id, seconds))

    def test_a_new_round_is_open_without_a_panel_and_closes_in_the_future(self, app_database):
        round_id = self.open_round()

        current = read(lambda c: gamble_repository.load_round(c, round_id))

        assert current.id == round_id
        assert (current.chat_id, current.status, current.message_id) == (-100, "open", None)
        assert current.is_open and not current.announced
        assert 290 < current.seconds_left <= 300
        assert (current.winner_id, current.prize) == (None, 0)

    def test_only_one_round_can_be_open_at_a_time(self, app_database):
        assert self.open_round() is not None

        assert self.open_round(chat_id=-200) is None

    def test_a_finished_round_makes_room_for_the_next_one(self, app_database):
        round_id = self.open_round()
        in_transaction(
            lambda c: gamble_repository.finish_round(
                c, round_id, status=gamble_repository.STATUS_SETTLED, winner_id=None, prize=0
            )
        )

        assert self.open_round() is not None

    def test_the_panel_message_is_attached_once(self, app_database):
        round_id = self.open_round()

        first = in_transaction(lambda c: gamble_repository.attach_message(c, round_id, 55))
        second = in_transaction(lambda c: gamble_repository.attach_message(c, round_id, 66))

        assert (first, second) == (True, False)
        assert read(lambda c: gamble_repository.load_round(c, round_id)).message_id == 55

    def test_only_a_round_without_a_panel_can_be_cancelled(self, app_database):
        no_panel = self.open_round()

        assert in_transaction(lambda c: gamble_repository.cancel_round(c, no_panel)) is True
        cancelled = read(lambda c: gamble_repository.load_round(c, no_panel))
        assert cancelled.status == "cancelled" and cancelled.announced

        with_panel = self.open_round()
        in_transaction(lambda c: gamble_repository.attach_message(c, with_panel, 55))
        assert in_transaction(lambda c: gamble_repository.cancel_round(c, with_panel)) is False
        assert read(lambda c: gamble_repository.load_round(c, with_panel)).is_open

    def test_a_player_can_bet_once_per_round_and_bets_keep_their_order(self, app_database):
        round_id = self.open_round()

        async def bet(connection, user_id, name, amount):
            return await gamble_repository.insert_bet(
                connection,
                round_id=round_id,
                user_id=user_id,
                username=name,
                amount=amount,
                op_key=f"gamble:{round_id}:bet:{user_id}",
            )

        assert in_transaction(lambda c: bet(c, 1, "alice", 5)) is True
        assert in_transaction(lambda c: bet(c, 2, "bob", 10)) is True
        assert in_transaction(lambda c: bet(c, 1, "alice", 20)) is False

        bets = read(lambda c: gamble_repository.load_bets(c, round_id))
        assert [(b.user_id, b.username, b.amount, b.op_key) for b in bets] == [
            (1, "alice", 5, f"gamble:{round_id}:bet:1"),
            (2, "bob", 10, f"gamble:{round_id}:bet:2"),
        ]

    def test_long_usernames_are_truncated_to_the_column_width(self, app_database):
        round_id = self.open_round()

        in_transaction(
            lambda c: gamble_repository.insert_bet(
                c,
                round_id=round_id,
                user_id=1,
                username="n" * 400,
                amount=5,
                op_key="gamble:x:bet:1",
            )
        )

        (bet,) = read(lambda c: gamble_repository.load_bets(c, round_id))
        assert len(bet.username) == 255

    def test_finishing_only_changes_a_round_that_is_still_open(self, app_database):
        round_id = self.open_round()

        in_transaction(
            lambda c: gamble_repository.finish_round(
                c, round_id, status="settled", winner_id=3, prize=25
            )
        )
        in_transaction(
            lambda c: gamble_repository.finish_round(
                c, round_id, status="refunded", winner_id=None, prize=0
            )
        )

        finished = read(lambda c: gamble_repository.load_round(c, round_id))
        assert (finished.status, finished.winner_id, finished.prize) == ("settled", 3, 25)
        assert not finished.is_open

    def test_due_and_unannounced_rounds_are_found_and_announcing_is_idempotent(self, app_database):
        due = self.open_round()
        execute(
            app_database,
            "UPDATE gamble_rounds SET closes_at = UTC_TIMESTAMP(6) - INTERVAL 1 SECOND",
        )

        assert read(gamble_repository.due_round_ids) == [due]
        assert read(gamble_repository.unannounced_round_ids) == []

        in_transaction(
            lambda c: gamble_repository.finish_round(
                c, due, status="settled", winner_id=None, prize=0
            )
        )
        assert read(gamble_repository.due_round_ids) == []
        assert read(gamble_repository.unannounced_round_ids) == [due]

        in_transaction(lambda c: gamble_repository.mark_announced(c, due))
        first = read(lambda c: gamble_repository.load_round(c, due))
        in_transaction(lambda c: gamble_repository.mark_announced(c, due))

        assert first.announced
        assert read(gamble_repository.unannounced_round_ids) == []

    def test_rounds_finished_long_ago_are_no_longer_retried(self, app_database):
        old = self.open_round()
        in_transaction(
            lambda c: gamble_repository.finish_round(
                c, old, status="settled", winner_id=None, prize=0
            )
        )
        execute(
            app_database,
            "UPDATE gamble_rounds SET settled_at = UTC_TIMESTAMP(6) - INTERVAL 2 DAY",
        )

        assert read(gamble_repository.unannounced_round_ids) == []

    def test_a_missing_round_is_none(self, app_database):
        assert read(lambda c: gamble_repository.load_round(c, 999)) is None
        assert read(lambda c: gamble_repository.load_bets(c, 999)) == ()

    def test_writes_roll_back_with_the_callers_transaction(self, app_database):
        async def work(connection):
            await gamble_repository.insert_round(connection, -100, 300)
            raise RuntimeError("injected failure")

        with pytest.raises(RuntimeError):
            in_transaction(work)

        assert fetch_scalar(app_database, "SELECT COUNT(*) FROM gamble_rounds") == 0


class TestRpsRepository:
    ALICE = Seat(user_id=1, name="alice", chat_id=-100, message_id=11)
    BOB = Seat(user_id=2, name="bob", chat_id=-100, message_id=12)

    def create(self, seconds=120):
        return in_transaction(
            lambda c: rps_repository.insert_game(
                c, self.ALICE, self.BOB, same_chat=True, seconds=seconds
            )
        )

    def test_a_new_game_has_both_seats_and_no_choices(self, app_database):
        game_id = self.create()

        game = read(lambda c: rps_repository.load_game(c, game_id))

        assert game.id == game_id and game.is_choosing and game.same_chat
        assert game.outcome is None and not game.announced
        assert (game.p1.user_id, game.p1.name, game.p1.message_id) == (1, "alice", 11)
        assert (game.p2.user_id, game.p2.name, game.p2.message_id) == (2, "bob", 12)
        assert game.p1.choice is None and game.p2.choice is None
        assert 110 < game.seconds_left <= 120
        assert game.seat_of(2) == game.p2 and game.opponent_of(2) == game.p1
        assert game.seat_of(3) is None

    def test_a_players_active_game_is_found_while_it_is_choosing(self, app_database):
        game_id = self.create()

        assert read(lambda c: rps_repository.has_choosing_game(c, 1)) is True
        assert read(lambda c: rps_repository.has_choosing_game(c, 2)) is True
        assert read(lambda c: rps_repository.has_choosing_game(c, 3)) is False
        assert read(lambda c: rps_repository.find_choosing_game(c, 2)).id == game_id
        assert read(lambda c: rps_repository.find_choosing_game(c, 3)) is None

        in_transaction(
            lambda c: rps_repository.finish_game(c, game_id, status="settled", outcome="draw")
        )

        assert read(lambda c: rps_repository.has_choosing_game(c, 1)) is False
        assert read(lambda c: rps_repository.find_choosing_game(c, 1)) is None

    def test_choices_are_stored_per_seat(self, app_database):
        game_id = self.create()

        in_transaction(lambda c: rps_repository.set_choice(c, game_id, "p1", "rock"))
        in_transaction(lambda c: rps_repository.set_choice(c, game_id, "p2", "paper"))

        game = read(lambda c: rps_repository.load_game(c, game_id))
        assert (game.p1.choice, game.p2.choice) == ("rock", "paper")

    def test_message_ids_are_recorded_and_unknown_columns_are_rejected(self, app_database):
        game_id = self.create()

        in_transaction(
            lambda c: rps_repository.update_message_ids(
                c, game_id, p1_private_msg_id=71, p2_private_msg_id=72
            )
        )

        game = read(lambda c: rps_repository.load_game(c, game_id))
        assert (game.p1.private_msg_id, game.p2.private_msg_id) == (71, 72)
        for columns in ({}, {"p1_choice": 1}, {"status; DROP TABLE rps_games": 1}):
            with pytest.raises(ValueError):
                in_transaction(
                    lambda c: rps_repository.update_message_ids(c, game_id, **columns)
                )

    def test_finishing_only_changes_a_game_that_is_still_choosing(self, app_database):
        game_id = self.create()

        in_transaction(
            lambda c: rps_repository.finish_game(c, game_id, status="settled", outcome="p1")
        )
        in_transaction(
            lambda c: rps_repository.finish_game(c, game_id, status="refunded", outcome="timeout")
        )

        game = read(lambda c: rps_repository.load_game(c, game_id))
        assert (game.status, game.outcome) == ("settled", "p1")
        assert fetch_scalar(app_database, "SELECT finished_at IS NOT NULL FROM rps_games") == 1

    def test_due_and_unannounced_games_are_found(self, app_database):
        game_id = self.create(seconds=-1)

        assert read(rps_repository.due_game_ids) == [game_id]
        assert read(rps_repository.unannounced_game_ids) == []

        in_transaction(
            lambda c: rps_repository.finish_game(c, game_id, status="refunded", outcome="timeout")
        )
        assert read(rps_repository.due_game_ids) == []
        assert read(rps_repository.unannounced_game_ids) == [game_id]

        in_transaction(lambda c: rps_repository.mark_announced(c, game_id))

        assert read(rps_repository.unannounced_game_ids) == []
        assert read(lambda c: rps_repository.load_game(c, game_id)).announced

    def test_a_missing_game_is_none(self, app_database):
        assert read(lambda c: rps_repository.load_game(c, 999)) is None

    def test_writes_roll_back_with_the_callers_transaction(self, app_database):
        async def work(connection):
            await rps_repository.insert_game(
                connection, self.ALICE, self.BOB, same_chat=False, seconds=120
            )
            raise RuntimeError("injected failure")

        with pytest.raises(RuntimeError):
            in_transaction(work)

        assert fetch_scalar(app_database, "SELECT COUNT(*) FROM rps_games") == 0


class TestOmikujiRepository:
    DAY = date(2026, 10, 5)

    def test_a_fortune_is_stored_per_user_and_day(self, app_database):
        seed_user(app_database, 1)
        assert run(omikuji_repository.get_fortune(1, self.DAY)) is None

        in_transaction(lambda c: omikuji_repository.save_fortune(c, 1, self.DAY, "大吉"))

        assert run(omikuji_repository.get_fortune(1, self.DAY)) == "大吉"
        assert run(omikuji_repository.get_fortune(1, date(2026, 10, 6))) is None

    def test_saving_again_replaces_the_fortune_of_the_same_day(self, app_database):
        seed_user(app_database, 1)
        in_transaction(lambda c: omikuji_repository.save_fortune(c, 1, self.DAY, "大吉"))

        in_transaction(lambda c: omikuji_repository.save_fortune(c, 1, self.DAY, "凶"))

        assert run(omikuji_repository.get_fortune(1, self.DAY)) == "凶"
        assert fetch_scalar(app_database, "SELECT COUNT(*) FROM user_omikuji") == 1

    def test_reads_see_uncommitted_rows_of_the_callers_transaction(self, app_database):
        seed_user(app_database, 1)

        async def work(connection):
            await omikuji_repository.save_fortune(connection, 1, self.DAY, "吉")
            inside = await omikuji_repository.get_fortune(1, self.DAY, connection=connection)
            outside = await omikuji_repository.get_fortune(1, self.DAY)
            return inside, outside

        assert in_transaction(work) == ("吉", None)


class TestRpgRepository:
    def test_a_character_is_created_with_the_starting_stats(self, app_database):
        seed_user(app_database, 1)
        assert run(rpg_repository.get_character(1)) is None

        in_transaction(lambda c: rpg_repository.insert_character(c, 1))

        character = run(rpg_repository.get_character(1))
        assert (character["level"], character["hp"], character["max_hp"]) == (1, 10, 10)
        assert (character["atk"], character["matk"], character["def"]) == (2, 0, 1)
        assert character["experience"] == 0 and character["allow_battle"] == 1

    def test_a_second_character_for_the_same_user_is_a_duplicate(self, app_database):
        seed_user(app_database, 1)
        in_transaction(lambda c: rpg_repository.insert_character(c, 1))

        with pytest.raises(IntegrityError):
            in_transaction(lambda c: rpg_repository.insert_character(c, 1))

    def test_character_fields_are_updated_and_the_row_count_is_reported(self, app_database):
        seed_user(app_database, 1)
        in_transaction(lambda c: rpg_repository.insert_character(c, 1))

        changed = in_transaction(
            lambda c: rpg_repository.update_character_fields(c, 1, {"hp": 4, "allow_battle": False})
        )
        missing = in_transaction(
            lambda c: rpg_repository.update_character_fields(c, 404, {"hp": 4})
        )

        character = run(rpg_repository.get_character(1))
        assert (changed, missing) == (1, 0)
        assert (character["hp"], character["allow_battle"]) == (4, 0)

    def test_nothing_to_update_touches_nothing(self, app_database):
        assert in_transaction(lambda c: rpg_repository.update_character_fields(c, 1, {})) == 0

    @pytest.mark.parametrize("name", ["hp; DROP TABLE user", "hp = 1, level", "", "1abc", "a b"])
    def test_field_names_are_validated_before_they_reach_sql(self, app_database, name):
        seed_user(app_database, 1)
        in_transaction(lambda c: rpg_repository.insert_character(c, 1))

        with pytest.raises(ValueError):
            in_transaction(lambda c: rpg_repository.update_character_fields(c, 1, {name: 1}))

        assert run(rpg_repository.get_character(1))["hp"] == 10

    def test_experience_is_added_not_overwritten(self, app_database):
        seed_user(app_database, 1)
        in_transaction(lambda c: rpg_repository.insert_character(c, 1))

        in_transaction(lambda c: rpg_repository.add_experience(c, 1, 30))
        in_transaction(lambda c: rpg_repository.add_experience(c, 1, 12))

        assert run(rpg_repository.get_character(1))["experience"] == 42

    def test_locking_the_character_returns_hp_and_its_limit(self, app_database):
        seed_user(app_database, 1)
        in_transaction(lambda c: rpg_repository.insert_character(c, 1))
        in_transaction(
            lambda c: rpg_repository.update_character_fields(c, 1, {"hp": 3, "max_hp": 15})
        )

        async def work(connection):
            return (
                await rpg_repository.lock_character_hp(connection, 1),
                await rpg_repository.lock_character_hp(connection, 404),
            )

        assert in_transaction(work) == ((3, 15), None)

    def seed_equipment(self, url):
        execute(
            url,
            "INSERT INTO rpg_equipment (id, name, type, atk_bonus, price) VALUES (1, 'sword', 'weapon', 3, 10)",
            "INSERT INTO rpg_equipment (id, name, type, def_bonus, price) VALUES (2, 'robe', 'armor', 2, 10)",
        )

    def test_equipment_slots_are_filled_cleared_and_listed_with_names(self, app_database):
        seed_user(app_database, 1)
        self.seed_equipment(app_database)

        # 玩家还没有装备记录时，放入装备会先建一条。
        in_transaction(lambda c: rpg_repository.set_equipment_slot(c, 1, "weapon_id", 1))
        in_transaction(lambda c: rpg_repository.set_equipment_slot(c, 1, "armor_id", 2))
        loadout = run(rpg_repository.get_player_equipment(1))
        assert (loadout["weapon_id"], loadout["weapon_name"]) == (1, "sword")
        assert (loadout["armor_id"], loadout["armor_name"]) == (2, "robe")
        assert loadout["offhand_id"] is None

        in_transaction(lambda c: rpg_repository.clear_equipment_slot(c, 1, "weapon_id"))
        assert run(rpg_repository.get_player_equipment(1))["weapon_id"] is None
        assert run(rpg_repository.get_equipment(2))["name"] == "robe"
        assert run(rpg_repository.get_equipment(99)) is None

    def test_a_player_without_equipment_has_no_loadout_until_one_is_created(self, app_database):
        seed_user(app_database, 1)
        assert run(rpg_repository.get_player_equipment(1)) is None

        in_transaction(lambda c: rpg_repository.insert_player_equipment(c, 1))

        assert run(rpg_repository.get_player_equipment(1))["weapon_id"] is None

    @pytest.mark.parametrize("column", ["weapon_id = 1, user_id", "level", ""])
    def test_slot_columns_are_restricted_to_the_known_slots(self, app_database, column):
        seed_user(app_database, 1)

        with pytest.raises(ValueError):
            in_transaction(lambda c: rpg_repository.set_equipment_slot(c, 1, column, 1))
        with pytest.raises(ValueError):
            in_transaction(lambda c: rpg_repository.clear_equipment_slot(c, 1, column))

    def test_equipment_stats_are_inserted_then_updated(self, app_database):
        seed_user(app_database, 1)
        assert run(rpg_repository.get_equipment_stats(1)) is None

        for atk in (3, 5):
            in_transaction(
                lambda c: rpg_repository.save_equipment_stats(
                    c, 1, atk_bonus=atk, def_bonus=1, hp_bonus=0, matk_bonus=2
                )
            )

        stats = run(rpg_repository.get_equipment_stats(1))
        assert (stats["total_atk_bonus"], stats["total_def_bonus"], stats["total_matk_bonus"]) == (
            5,
            1,
            2,
        )
        assert fetch_scalar(app_database, "SELECT COUNT(*) FROM rpg_player_equipment_stats") == 1

    def test_inventory_quantities_are_added_reduced_and_deleted(self, app_database):
        seed_user(app_database, 1)
        execute(
            app_database,
            "INSERT INTO rpg_items (id, name, type, price) VALUES (1, 'potion', 'consumable', 5)",
        )
        assert run(rpg_repository.get_inventory(1)) == []
        assert run(rpg_repository.get_item(1))["name"] == "potion"
        assert run(rpg_repository.get_item(99)) is None

        in_transaction(lambda c: rpg_repository.insert_item(c, 1, 1, 2))
        in_transaction(lambda c: rpg_repository.increase_item_quantity(c, 1, 1, 3))
        in_transaction(lambda c: rpg_repository.decrease_item_quantity(c, 1, 1, 1))

        (entry,) = run(rpg_repository.get_inventory(1))
        assert (entry["item_id"], entry["quantity"], entry["name"], entry["type"]) == (
            1,
            4,
            "potion",
            "consumable",
        )

        in_transaction(lambda c: rpg_repository.delete_item(c, 1, 1))
        assert run(rpg_repository.get_inventory(1)) == []

    def test_an_item_can_only_have_one_inventory_row_per_user(self, app_database):
        seed_user(app_database, 1)
        execute(
            app_database,
            "INSERT INTO rpg_items (id, name, type, price) VALUES (1, 'potion', 'consumable', 5)",
        )
        in_transaction(lambda c: rpg_repository.insert_item(c, 1, 1, 1))

        with pytest.raises(IntegrityError):
            in_transaction(lambda c: rpg_repository.insert_item(c, 1, 1, 1))

    def test_writes_roll_back_with_the_callers_transaction(self, app_database):
        seed_user(app_database, 1)

        async def work(connection):
            await rpg_repository.insert_character(connection, 1)
            raise RuntimeError("injected failure")

        with pytest.raises(RuntimeError):
            in_transaction(work)

        assert fetch(app_database, "SELECT * FROM rpg_characters") == []
