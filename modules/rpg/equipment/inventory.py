import logging
import asyncio
from typing import Dict, List, Optional, Union, Tuple
import sys
import os

# 修正导入路径
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import mysql_connection
from rpg.utils import rpg_db_executor

# 道具栏容量上限
INVENTORY_CAPACITY = 10


# --- 道具相关功能 ---
async def get_player_inventory(user_id: int) -> List[Dict]:
    """获取玩家的道具列表"""
    loop = asyncio.get_running_loop()
    
    def db_operation():
        connection = mysql_connection.create_connection()
        if not connection:
            logging.error("无法连接到数据库 (get_player_inventory)")
            return []
            
        cursor = None
        try:
            query = """
            SELECT pi.id, pi.user_id, pi.item_id, pi.quantity, 
                   i.name, i.type, i.effect, i.description, i.price
            FROM rpg_player_inventory pi
            JOIN rpg_items i ON pi.item_id = i.id
            WHERE pi.user_id = %s
            """
            cursor = connection.cursor(dictionary=True)
            cursor.execute(query, (user_id,))
            results = cursor.fetchall()
            
            if not results:
                return []
                
            return list(results)
            
        except Exception as e:
            logging.error(f"获取玩家道具失败: {e}")
            return []
        finally:
            if cursor:
                cursor.close()
            if connection and connection.is_connected():
                connection.close()
    
    return await loop.run_in_executor(rpg_db_executor, db_operation)


async def get_item_details(item_id: int) -> Dict:
    """获取道具详细信息"""
    if not item_id:
        return None
        
    loop = asyncio.get_running_loop()
    
    def db_operation():
        connection = mysql_connection.create_connection()
        if not connection:
            logging.error("无法连接到数据库 (get_item_details)")
            return None
            
        cursor = None
        try:
            query = """
            SELECT * FROM rpg_items WHERE id = %s
            """
            cursor = connection.cursor(dictionary=True)
            cursor.execute(query, (item_id,))
            result = cursor.fetchone()
            
            return result
            
        except Exception as e:
            logging.error(f"获取道具详情失败: {e}")
            return None
        finally:
            if cursor:
                cursor.close()
            if connection and connection.is_connected():
                connection.close()
    
    return await loop.run_in_executor(rpg_db_executor, db_operation)


async def add_item_to_inventory(user_id: int, item_id: int, quantity: int = 1) -> Tuple[bool, str]:
    """向玩家道具栏添加道具"""
    try:
        # 检查道具是否存在
        item = await get_item_details(item_id)
        if not item:
            return False, "道具不存在"
            
        # 获取玩家当前道具列表
        inventory = await get_player_inventory(user_id)
        
        # 检查是否已有该道具
        existing_item = next((i for i in inventory if i['item_id'] == item_id), None)
        
        loop = asyncio.get_running_loop()
        
        def db_operation():
            connection = mysql_connection.create_connection()
            if not connection:
                logging.error("无法连接到数据库 (add_item_to_inventory)")
                return (False, "数据库连接失败")
                
            cursor = None
            try:
                if existing_item:
                    # 已有道具，更新数量
                    query = """
                    UPDATE rpg_player_inventory
                    SET quantity = quantity + %s
                    WHERE user_id = %s AND item_id = %s
                    """
                    cursor = connection.cursor()
                    cursor.execute(query, (quantity, user_id, item_id))
                else:
                    # 检查道具栏是否已满
                    if len(inventory) >= INVENTORY_CAPACITY:
                        return (False, f"道具栏已满（最多{INVENTORY_CAPACITY}个）")
                        
                    # 添加新道具
                    query = """
                    INSERT INTO rpg_player_inventory (user_id, item_id, quantity)
                    VALUES (%s, %s, %s)
                    """
                    cursor = connection.cursor()
                    cursor.execute(query, (user_id, item_id, quantity))
                
                connection.commit()
                return (True, f"成功获得 {quantity} 个 {item['name']}")
                
            except Exception as e:
                logging.error(f"添加道具失败: {e}")
                if connection.in_transaction:
                    connection.rollback()
                return (False, f"添加道具失败: {str(e)}")
            finally:
                if cursor:
                    cursor.close()
                if connection and connection.is_connected():
                    connection.close()
        
        return await loop.run_in_executor(rpg_db_executor, db_operation)
    except Exception as e:
        logging.error(f"添加道具过程中出错: {e}")
        return False, f"添加道具出错: {str(e)}"


async def remove_item_from_inventory(user_id: int, item_id: int, quantity: int = 1) -> Tuple[bool, str]:
    """从玩家道具栏移除道具"""
    try:
        # 获取玩家当前道具列表
        inventory = await get_player_inventory(user_id)
        
        # 检查是否有该道具
        existing_item = next((i for i in inventory if i['item_id'] == item_id), None)
        if not existing_item:
            return False, "你没有这个道具"
            
        # 检查数量是否足够
        if existing_item['quantity'] < quantity:
            return False, f"道具数量不足（需要{quantity}个，但只有{existing_item['quantity']}个）"
            
        loop = asyncio.get_running_loop()
        
        def db_operation():
            connection = mysql_connection.create_connection()
            if not connection:
                logging.error("无法连接到数据库 (remove_item_from_inventory)")
                return (False, "数据库连接失败")
                
            cursor = None
            try:
                if existing_item['quantity'] == quantity:
                    # 刚好用完，删除记录
                    query = """
                    DELETE FROM rpg_player_inventory
                    WHERE user_id = %s AND item_id = %s
                    """
                    cursor = connection.cursor()
                    cursor.execute(query, (user_id, item_id))
                else:
                    # 减少数量
                    query = """
                    UPDATE rpg_player_inventory
                    SET quantity = quantity - %s
                    WHERE user_id = %s AND item_id = %s
                    """
                    cursor = connection.cursor()
                    cursor.execute(query, (quantity, user_id, item_id))
                
                connection.commit()
                return (True, f"移除了 {quantity} 个 {existing_item['name']}")
                
            except Exception as e:
                logging.error(f"移除道具失败: {e}")
                if connection.in_transaction:
                    connection.rollback()
                return (False, f"移除道具失败: {str(e)}")
            finally:
                if cursor:
                    cursor.close()
                if connection and connection.is_connected():
                    connection.close()
        
        return await loop.run_in_executor(rpg_db_executor, db_operation)
    except Exception as e:
        logging.error(f"移除道具过程中出错: {e}")
        return False, f"移除道具出错: {str(e)}"


async def use_item(user_id: int, item_id: int) -> Tuple[bool, str]:
    """使用道具的功能"""
    try:
        # 获取道具详情
        item = await get_item_details(item_id)
        if not item:
            return False, "道具不存在"
            
        # 检查是否是可使用的道具
        if item['type'] != 'consumable':
            return False, f"{item['name']} 不是可使用的消耗品"
            
        # 检查玩家是否有该道具
        inventory = await get_player_inventory(user_id)
        existing_item = next((i for i in inventory if i['item_id'] == item_id), None)
        if not existing_item:
            return False, "你没有这个道具"
            
        # 根据道具效果执行相应操作
        effect = item['effect']
        result_message = f"使用了 {item['name']}"
        
        # 这里可以根据不同道具类型执行不同的逻辑
        # 例如：恢复HP、增加临时属性等
        # 暂时留空，后续可添加具体实现
        
        # 使用后减少道具数量
        success, message = await remove_item_from_inventory(user_id, item_id, 1)
        if not success:
            return False, message
            
        return True, result_message
    except Exception as e:
        logging.error(f"使用道具过程中出错: {e}")
        return False, f"使用道具出错: {str(e)}"


def item_type_to_chinese(item_type: str) -> str:
    """将道具类型转换为中文描述"""
    type_map = {
        'consumable': '消耗品',
        'material': '材料',
        'quest': '任务物品'
    }
    return type_map.get(item_type, item_type) 