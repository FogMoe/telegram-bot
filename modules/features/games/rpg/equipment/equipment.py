import logging
from typing import Dict, List, Optional, Union, Tuple

from core import mysql_connection


# --- 装备相关功能 ---
async def get_player_equipment(user_id: int) -> Dict:
    """获取玩家当前装备信息"""
    try:
        result = await mysql_connection.fetch_one(
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

        if not result:
            await mysql_connection.execute(
                "INSERT INTO rpg_player_equipment (user_id) VALUES (%s)",
                (user_id,),
            )
            return {
                'user_id': user_id,
                'weapon_id': None,
                'offhand_id': None,
                'armor_id': None,
                'treasure1_id': None,
                'treasure2_id': None,
                'weapon_name': None,
                'offhand_name': None,
                'armor_name': None,
                'treasure1_name': None,
                'treasure2_name': None
            }

        return dict(result)
    except Exception as e:
        logging.error(f"获取玩家装备信息失败: {e}")
        return None


async def get_equipment_details(equipment_id: int) -> Dict:
    """获取装备详细信息"""
    if not equipment_id:
        return None

    try:
        result = await mysql_connection.fetch_one(
            "SELECT * FROM rpg_equipment WHERE id = %s",
            (equipment_id,),
            mapping=True,
        )
        return dict(result) if result else None
    except Exception as e:
        logging.error(f"获取装备详情失败: {e}")
        return None


async def equip_item(user_id: int, equipment_id: int) -> Tuple[bool, str]:
    """为玩家装备物品"""
    try:
        # 检查装备是否存在
        equipment = await get_equipment_details(equipment_id)
        if not equipment:
            return False, "装备不存在"
            
        equipment_type = equipment['type']
        
        # 获取玩家当前装备
        current_equipment = await get_player_equipment(user_id)
        if not current_equipment:
            return False, "获取玩家装备信息失败"
            
        # 检查玩家是否拥有此装备（未来实现）
        # TODO: 检查玩家背包中是否有此装备
            
        # 确定要更新的装备槽位
        slot_column = f"{equipment_type}_id"
        if slot_column not in ['weapon_id', 'offhand_id', 'armor_id', 'treasure1_id', 'treasure2_id']:
            return False, f"不支持的装备类型: {equipment_type}"
            
        # 更新玩家装备
        async with mysql_connection.transaction() as connection:
            query = f"""
            UPDATE rpg_player_equipment 
            SET {slot_column} = %s
            WHERE user_id = %s
            """
            result = await connection.exec_driver_sql(query, (equipment_id, user_id))
            if result.rowcount == 0:
                insert_query = f"""
                INSERT INTO rpg_player_equipment (user_id, {slot_column})
                VALUES (%s, %s)
                """
                await connection.exec_driver_sql(insert_query, (user_id, equipment_id))

        result = (True, f"成功装备 {equipment['name']}")
        
        # 更新装备统计数据
        if result[0]:
            await update_equipment_stats(user_id)
            
        return result
                
    except Exception as e:
        logging.error(f"装备物品过程中出错: {e}")
        return False, f"装备出错: {str(e)}"


async def unequip_item(user_id: int, equipment_type: str) -> Tuple[bool, str]:
    """卸下玩家装备"""
    try:
        # 验证装备类型
        if equipment_type not in ['weapon', 'offhand', 'armor', 'treasure1', 'treasure2']:
            return False, f"不支持的装备类型: {equipment_type}"
            
        # 获取玩家当前装备
        current_equipment = await get_player_equipment(user_id)
        if not current_equipment:
            return False, "获取玩家装备信息失败"
            
        # 检查该位置是否有装备
        slot_column = f"{equipment_type}_id"
        slot_name = f"{equipment_type}_name"
        
        if not current_equipment[slot_column]:
            return False, f"你当前没有装备{equipment_type_to_chinese(equipment_type)}"
            
        equipment_name = current_equipment[slot_name]
            
        # 更新玩家装备
        async with mysql_connection.transaction() as connection:
            query = f"""
            UPDATE rpg_player_equipment 
            SET {slot_column} = NULL
            WHERE user_id = %s
            """
            await connection.exec_driver_sql(query, (user_id,))

        result = (True, f"成功卸下 {equipment_name}")
        
        # 更新装备统计数据
        if result[0]:
            await update_equipment_stats(user_id)
            
        return result
    except Exception as e:
        logging.error(f"卸下装备过程中出错: {e}")
        return False, f"卸下装备出错: {str(e)}"


async def update_equipment_stats(user_id: int) -> bool:
    """更新玩家装备带来的属性加成"""
    try:
        # 获取玩家当前装备
        current_equipment = await get_player_equipment(user_id)
        if not current_equipment:
            return False
            
        # 计算各项属性加成总和
        total_atk_bonus = 0
        total_def_bonus = 0
        total_hp_bonus = 0
        total_matk_bonus = 0
        
        # 检查每个装备槽位
        for slot in ['weapon', 'offhand', 'armor', 'treasure1', 'treasure2']:
            equipment_id = current_equipment[f"{slot}_id"]
            if equipment_id:
                equipment = await get_equipment_details(equipment_id)
                if equipment:
                    total_atk_bonus += equipment['atk_bonus']
                    total_def_bonus += equipment['def_bonus']
                    total_hp_bonus += equipment['hp_bonus']
                    total_matk_bonus += equipment['matk_bonus']
        
        # 更新装备统计缓存表
        async with mysql_connection.transaction() as connection:
            query = """
            UPDATE rpg_player_equipment_stats
            SET total_atk_bonus = %s, total_def_bonus = %s, 
                total_hp_bonus = %s, total_matk_bonus = %s
            WHERE user_id = %s
            """
            result = await connection.exec_driver_sql(
                query,
                (
                    total_atk_bonus, total_def_bonus,
                    total_hp_bonus, total_matk_bonus,
                    user_id,
                ),
            )
            if result.rowcount == 0:
                insert_query = """
                INSERT INTO rpg_player_equipment_stats
                (user_id, total_atk_bonus, total_def_bonus, total_hp_bonus, total_matk_bonus)
                VALUES (%s, %s, %s, %s, %s)
                """
                await connection.exec_driver_sql(
                    insert_query,
                    (
                        user_id, total_atk_bonus, total_def_bonus,
                        total_hp_bonus, total_matk_bonus,
                    ),
                )
        return True
            
    except Exception as e:
        logging.error(f"更新装备统计数据时出错: {e}")
        return False


async def get_equipment_stats(user_id: int) -> Dict:
    """获取玩家装备的属性加成总和"""
    try:
        result = await mysql_connection.fetch_one(
            """
            SELECT * FROM rpg_player_equipment_stats
            WHERE user_id = %s
            """,
            (user_id,),
            mapping=True,
        )

        if not result:
            return {
                'user_id': user_id,
                'total_atk_bonus': 0,
                'total_def_bonus': 0,
                'total_hp_bonus': 0,
                'total_matk_bonus': 0
            }

        return dict(result)
    except Exception as e:
        logging.error(f"获取装备属性加成数据失败: {e}")
        return None


def equipment_type_to_chinese(equipment_type: str) -> str:
    """将装备类型转换为中文描述"""
    type_map = {
        'weapon': '武器',
        'offhand': '副武器',
        'armor': '护甲',
        'treasure1': '宝物1',
        'treasure2': '宝物2'
    }
    return type_map.get(equipment_type, equipment_type) 
