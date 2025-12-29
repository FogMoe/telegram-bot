import logging
import asyncio
from typing import Dict, List, Optional, Union, Tuple

from core import mysql_connection

from ..utils import rpg_db_executor


# --- 装备相关功能 ---
async def get_player_equipment(user_id: int) -> Dict:
    """获取玩家当前装备信息"""
    loop = asyncio.get_running_loop()
    
    def db_operation():
        connection = mysql_connection.create_connection()
        if not connection:
            logging.error("无法连接到数据库 (get_player_equipment)")
            return None
            
        cursor = None
        try:
            # 获取玩家装备信息
            query = """
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
            """
            cursor = connection.cursor(dictionary=True)
            cursor.execute(query, (user_id,))
            result = cursor.fetchone()

            if not result:
                # 如果玩家没有装备记录，创建一个空记录
                insert_query = """
                INSERT INTO rpg_player_equipment (user_id)
                VALUES (%s)
                """
                cursor.execute(insert_query, (user_id,))
                connection.commit()
                
                # 返回空装备信息
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
            
            return result
            
        except Exception as e:
            logging.error(f"获取玩家装备信息失败: {e}")
            if connection.in_transaction:
                connection.rollback()
            return None
        finally:
            if cursor:
                cursor.close()
            if connection and connection.is_connected():
                connection.close()
    
    return await loop.run_in_executor(rpg_db_executor, db_operation)


async def get_equipment_details(equipment_id: int) -> Dict:
    """获取装备详细信息"""
    if not equipment_id:
        return None
        
    loop = asyncio.get_running_loop()
    
    def db_operation():
        connection = mysql_connection.create_connection()
        if not connection:
            logging.error("无法连接到数据库 (get_equipment_details)")
            return None
            
        cursor = None
        try:
            query = """
            SELECT * FROM rpg_equipment WHERE id = %s
            """
            cursor = connection.cursor(dictionary=True)
            cursor.execute(query, (equipment_id,))
            result = cursor.fetchone()
            
            return result
            
        except Exception as e:
            logging.error(f"获取装备详情失败: {e}")
            return None
        finally:
            if cursor:
                cursor.close()
            if connection and connection.is_connected():
                connection.close()
    
    return await loop.run_in_executor(rpg_db_executor, db_operation)


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
        loop = asyncio.get_running_loop()
        
        def db_operation():
            connection = mysql_connection.create_connection()
            if not connection:
                logging.error("无法连接到数据库 (equip_item)")
                return (False, "数据库连接失败")
                
            cursor = None
            try:
                query = f"""
                UPDATE rpg_player_equipment 
                SET {slot_column} = %s
                WHERE user_id = %s
                """
                cursor = connection.cursor()
                cursor.execute(query, (equipment_id, user_id))
                
                if cursor.rowcount == 0:
                    # 如果没有更新行，创建一个新记录
                    columns = ["user_id", slot_column]
                    values = [user_id, equipment_id]
                    
                    insert_query = f"""
                    INSERT INTO rpg_player_equipment (user_id, {slot_column})
                    VALUES (%s, %s)
                    """
                    cursor.execute(insert_query, (user_id, equipment_id))
                
                connection.commit()
                return (True, f"成功装备 {equipment['name']}")
                
            except Exception as e:
                logging.error(f"装备物品失败: {e}")
                if connection.in_transaction:
                    connection.rollback()
                return (False, f"装备失败: {str(e)}")
            finally:
                if cursor:
                    cursor.close()
                if connection and connection.is_connected():
                    connection.close()
        
        result = await loop.run_in_executor(rpg_db_executor, db_operation)
        
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
        loop = asyncio.get_running_loop()
        
        def db_operation():
            connection = mysql_connection.create_connection()
            if not connection:
                logging.error("无法连接到数据库 (unequip_item)")
                return (False, "数据库连接失败")
                
            cursor = None
            try:
                query = f"""
                UPDATE rpg_player_equipment 
                SET {slot_column} = NULL
                WHERE user_id = %s
                """
                cursor = connection.cursor()
                cursor.execute(query, (user_id,))
                connection.commit()
                
                return (True, f"成功卸下 {equipment_name}")
                
            except Exception as e:
                logging.error(f"卸下装备失败: {e}")
                if connection.in_transaction:
                    connection.rollback()
                return (False, f"卸下装备失败: {str(e)}")
            finally:
                if cursor:
                    cursor.close()
                if connection and connection.is_connected():
                    connection.close()
        
        result = await loop.run_in_executor(rpg_db_executor, db_operation)
        
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
        loop = asyncio.get_running_loop()
        
        def db_operation():
            connection = mysql_connection.create_connection()
            if not connection:
                logging.error("无法连接到数据库 (update_equipment_stats)")
                return False
                
            cursor = None
            try:
                # 尝试更新现有记录
                query = """
                UPDATE rpg_player_equipment_stats
                SET total_atk_bonus = %s, total_def_bonus = %s, 
                    total_hp_bonus = %s, total_matk_bonus = %s
                WHERE user_id = %s
                """
                cursor = connection.cursor()
                cursor.execute(query, (
                    total_atk_bonus, total_def_bonus, 
                    total_hp_bonus, total_matk_bonus, 
                    user_id
                ))
                
                if cursor.rowcount == 0:
                    # 如果没有更新任何记录，插入新记录
                    insert_query = """
                    INSERT INTO rpg_player_equipment_stats
                    (user_id, total_atk_bonus, total_def_bonus, total_hp_bonus, total_matk_bonus)
                    VALUES (%s, %s, %s, %s, %s)
                    """
                    cursor.execute(insert_query, (
                        user_id, total_atk_bonus, total_def_bonus, 
                        total_hp_bonus, total_matk_bonus
                    ))
                
                connection.commit()
                return True
            except Exception as e:
                logging.error(f"更新装备统计失败: {e}")
                if connection.in_transaction:
                    connection.rollback()
                return False
            finally:
                if cursor:
                    cursor.close()
                if connection and connection.is_connected():
                    connection.close()
        
        return await loop.run_in_executor(rpg_db_executor, db_operation)
            
    except Exception as e:
        logging.error(f"更新装备统计数据时出错: {e}")
        return False


async def get_equipment_stats(user_id: int) -> Dict:
    """获取玩家装备的属性加成总和"""
    loop = asyncio.get_running_loop()
    
    def db_operation():
        connection = mysql_connection.create_connection()
        if not connection:
            logging.error("无法连接到数据库 (get_equipment_stats)")
            return None
            
        cursor = None
        try:
            query = """
            SELECT * FROM rpg_player_equipment_stats
            WHERE user_id = %s
            """
            cursor = connection.cursor(dictionary=True)
            cursor.execute(query, (user_id,))
            result = cursor.fetchone()
            
            if not result:
                return {
                    'user_id': user_id,
                    'total_atk_bonus': 0,
                    'total_def_bonus': 0,
                    'total_hp_bonus': 0,
                    'total_matk_bonus': 0
                }
                
            return result
            
        except Exception as e:
            logging.error(f"获取装备属性加成数据失败: {e}")
            return None
        finally:
            if cursor:
                cursor.close()
            if connection and connection.is_connected():
                connection.close()
    
    return await loop.run_in_executor(rpg_db_executor, db_operation)


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
