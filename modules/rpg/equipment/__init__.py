# RPG 装备模块初始化文件
from .equipment import (
    get_player_equipment,
    get_equipment_details,
    equip_item,
    unequip_item,
    update_equipment_stats,
    get_equipment_stats,
    equipment_type_to_chinese
)

from .inventory import (
    get_player_inventory,
    get_item_details,
    add_item_to_inventory,
    remove_item_from_inventory,
    use_item,
    item_type_to_chinese,
    INVENTORY_CAPACITY
)

# 导出的函数和类
__all__ = [
    # 装备系统
    'get_player_equipment',
    'get_equipment_details',
    'equip_item',
    'unequip_item',
    'update_equipment_stats',
    'get_equipment_stats',
    'equipment_type_to_chinese',
    
    # 道具系统
    'get_player_inventory',
    'get_item_details',
    'add_item_to_inventory',
    'remove_item_from_inventory',
    'use_item',
    'item_type_to_chinese',
    'INVENTORY_CAPACITY'
] 