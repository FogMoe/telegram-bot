"""RPG 的角色、装备与道具（`rpg_characters`、`rpg_equipment`、`rpg_player_equipment*`、
`rpg_items`、`rpg_player_inventory`）。

金币结算与角色状态的事务在 `features/games/rpg/settlement.py`；这里是单条语句级别的读写。
记录以字典返回：RPG 各处按列名取值，列的集合由表结构决定。
按名字拼进 SQL 的列只有两处（角色字段、装备槽位），都先对照白名单或标识符规则校验。
"""

import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_telegram_bot.core import sql

_FIELD_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
EQUIPMENT_SLOT_COLUMNS = ("weapon_id", "offhand_id", "armor_id", "treasure1_id", "treasure2_id")


def is_valid_field_name(name: str) -> bool:
    return bool(_FIELD_NAME.match(name))


# ---------------------------------------------------------------------------
# 角色
# ---------------------------------------------------------------------------


async def get_character(
    user_id: int, *, connection: AsyncConnection | None = None
) -> dict[str, Any] | None:
    row = await sql.fetch_one(
        "SELECT * FROM rpg_characters WHERE user_id = %s",
        (user_id,),
        mapping=True,
        connection=connection,
    )
    return dict(row) if row else None


async def insert_character(connection: AsyncConnection, user_id: int) -> None:
    """创建初始角色；已有角色时抛 `IntegrityError`。"""
    await connection.exec_driver_sql(
        """
        INSERT INTO rpg_characters (user_id, level, hp, max_hp, atk, matk, def, experience, allow_battle)
        VALUES (%s, 1, 10, 10, 2, 0, 1, 0, TRUE)
        """,
        (user_id,),
    )


async def update_character_fields(
    connection: AsyncConnection, user_id: int, updates: dict[str, Any]
) -> int:
    """更新角色字段，返回影响行数。字段名不合法抛 `ValueError`，调用方的事务随之回滚。"""
    if not updates:
        return 0
    for key in updates:
        if not is_valid_field_name(key):
            raise ValueError(f"非法字段名: {key}")
    assignments = ", ".join(f"{key} = %s" for key in updates)
    result = await connection.exec_driver_sql(
        f"UPDATE rpg_characters SET {assignments} WHERE user_id = %s",
        (*updates.values(), user_id),
    )
    return int(result.rowcount)


async def add_experience(connection: AsyncConnection, user_id: int, amount: int) -> None:
    """经验用增量写入，不依赖调用方先前读到的旧值。"""
    await connection.exec_driver_sql(
        "UPDATE rpg_characters SET experience = experience + %s WHERE user_id = %s",
        (amount, user_id),
    )


async def lock_character_hp(
    connection: AsyncConnection, user_id: int
) -> tuple[int, int] | None:
    """锁住角色行并返回 (当前生命值, 生命值上限)；没有角色返回 None。"""
    row = (
        await connection.exec_driver_sql(
            "SELECT hp, max_hp FROM rpg_characters WHERE user_id = %s FOR UPDATE",
            (user_id,),
        )
    ).first()
    if row is None:
        return None
    return row[0], int(row[1])


# ---------------------------------------------------------------------------
# 装备
# ---------------------------------------------------------------------------


async def get_player_equipment(user_id: int) -> dict[str, Any] | None:
    """玩家当前五个槽位的装备 id 与名称；还没有记录返回 None。"""
    row = await sql.fetch_one(
        """
        SELECT
            pe.user_id,
            pe.weapon_id,
            pe.offhand_id,
            pe.armor_id,
            pe.treasure1_id,
            pe.treasure2_id,
            w.name as weapon_name,
            o.name as offhand_name,
            a.name as armor_name,
            t1.name as treasure1_name,
            t2.name as treasure2_name
        FROM rpg_player_equipment pe
        LEFT JOIN rpg_equipment w ON pe.weapon_id = w.id
        LEFT JOIN rpg_equipment o ON pe.offhand_id = o.id
        LEFT JOIN rpg_equipment a ON pe.armor_id = a.id
        LEFT JOIN rpg_equipment t1 ON pe.treasure1_id = t1.id
        LEFT JOIN rpg_equipment t2 ON pe.treasure2_id = t2.id
        WHERE pe.user_id = %s
        """,
        (user_id,),
        mapping=True,
    )
    return dict(row) if row else None


async def insert_player_equipment(connection: AsyncConnection, user_id: int) -> None:
    await connection.exec_driver_sql(
        "INSERT INTO rpg_player_equipment (user_id) VALUES (%s)",
        (user_id,),
    )


async def get_equipment(equipment_id: int) -> dict[str, Any] | None:
    row = await sql.fetch_one(
        "SELECT * FROM rpg_equipment WHERE id = %s",
        (equipment_id,),
        mapping=True,
    )
    return dict(row) if row else None


def _check_slot_column(slot_column: str) -> None:
    if slot_column not in EQUIPMENT_SLOT_COLUMNS:
        raise ValueError(f"不支持的装备槽位: {slot_column}")


async def set_equipment_slot(
    connection: AsyncConnection, user_id: int, slot_column: str, equipment_id: int
) -> None:
    """把装备放进槽位；玩家还没有装备记录时先建一条。"""
    _check_slot_column(slot_column)
    result = await connection.exec_driver_sql(
        f"""
            UPDATE rpg_player_equipment
            SET {slot_column} = %s
            WHERE user_id = %s
            """,
        (equipment_id, user_id),
    )
    if result.rowcount == 0:
        await connection.exec_driver_sql(
            f"""
                INSERT INTO rpg_player_equipment (user_id, {slot_column})
                VALUES (%s, %s)
                """,
            (user_id, equipment_id),
        )


async def clear_equipment_slot(
    connection: AsyncConnection, user_id: int, slot_column: str
) -> None:
    _check_slot_column(slot_column)
    await connection.exec_driver_sql(
        f"""
            UPDATE rpg_player_equipment
            SET {slot_column} = NULL
            WHERE user_id = %s
            """,
        (user_id,),
    )


async def save_equipment_stats(
    connection: AsyncConnection,
    user_id: int,
    *,
    atk_bonus: int,
    def_bonus: int,
    hp_bonus: int,
    matk_bonus: int,
) -> None:
    """写入装备带来的属性加成缓存；没有记录时新建。"""
    result = await connection.exec_driver_sql(
        """
            UPDATE rpg_player_equipment_stats
            SET total_atk_bonus = %s, total_def_bonus = %s,
                total_hp_bonus = %s, total_matk_bonus = %s
            WHERE user_id = %s
            """,
        (atk_bonus, def_bonus, hp_bonus, matk_bonus, user_id),
    )
    if result.rowcount == 0:
        await connection.exec_driver_sql(
            """
                INSERT INTO rpg_player_equipment_stats
                (user_id, total_atk_bonus, total_def_bonus, total_hp_bonus, total_matk_bonus)
                VALUES (%s, %s, %s, %s, %s)
                """,
            (user_id, atk_bonus, def_bonus, hp_bonus, matk_bonus),
        )


async def get_equipment_stats(user_id: int) -> dict[str, Any] | None:
    row = await sql.fetch_one(
        """
            SELECT * FROM rpg_player_equipment_stats
            WHERE user_id = %s
            """,
        (user_id,),
        mapping=True,
    )
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# 道具
# ---------------------------------------------------------------------------


async def get_inventory(user_id: int) -> list[dict[str, Any]]:
    rows = await sql.fetch_all(
        """
        SELECT pi.id, pi.user_id, pi.item_id, pi.quantity,
               i.name, i.type, i.effect, i.description, i.price
        FROM rpg_player_inventory pi
        JOIN rpg_items i ON pi.item_id = i.id
        WHERE pi.user_id = %s
        """,
        (user_id,),
        mapping=True,
    )
    return [dict(row) for row in rows]


async def get_item(item_id: int) -> dict[str, Any] | None:
    row = await sql.fetch_one(
        "SELECT * FROM rpg_items WHERE id = %s",
        (item_id,),
        mapping=True,
    )
    return dict(row) if row else None


async def increase_item_quantity(
    connection: AsyncConnection, user_id: int, item_id: int, quantity: int
) -> None:
    await connection.exec_driver_sql(
        """
                    UPDATE rpg_player_inventory
                    SET quantity = quantity + %s
                    WHERE user_id = %s AND item_id = %s
                    """,
        (quantity, user_id, item_id),
    )


async def insert_item(
    connection: AsyncConnection, user_id: int, item_id: int, quantity: int
) -> None:
    await connection.exec_driver_sql(
        """
                INSERT INTO rpg_player_inventory (user_id, item_id, quantity)
                VALUES (%s, %s, %s)
                """,
        (user_id, item_id, quantity),
    )


async def decrease_item_quantity(
    connection: AsyncConnection, user_id: int, item_id: int, quantity: int
) -> None:
    await connection.exec_driver_sql(
        """
                    UPDATE rpg_player_inventory
                    SET quantity = quantity - %s
                    WHERE user_id = %s AND item_id = %s
                    """,
        (quantity, user_id, item_id),
    )


async def delete_item(connection: AsyncConnection, user_id: int, item_id: int) -> None:
    await connection.exec_driver_sql(
        """
                    DELETE FROM rpg_player_inventory
                    WHERE user_id = %s AND item_id = %s
                    """,
        (user_id, item_id),
    )
