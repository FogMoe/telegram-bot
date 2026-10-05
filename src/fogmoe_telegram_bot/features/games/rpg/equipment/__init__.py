# RPG 装备模块初始化文件
from .equipment import (
    equip_item,
    equipment_type_to_chinese,
    get_equipment_details,
    get_equipment_stats,
    get_player_equipment,
    unequip_item,
    update_equipment_stats,
)
from .inventory import (
    INVENTORY_CAPACITY,
    add_item_to_inventory,
    get_item_details,
    get_player_inventory,
    item_type_to_chinese,
    remove_item_from_inventory,
    use_item,
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
