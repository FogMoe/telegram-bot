# RPG 模块初始化文件

# 导入主要功能和类
from .battles import initiate_battle, run_battle
from .characters import (
    check_and_process_level_up,
    create_character,
    get_character,
    get_user_id_by_username,
    heal_character,
    set_battle_allowance,
    update_character_stats,
)
from .commands import rpg_command_handler, setup_rpg_handlers

# 导入装备和道具系统
from .equipment import (
    INVENTORY_CAPACITY,
    add_item_to_inventory,
    equip_item,
    equipment_type_to_chinese,
    get_equipment_details,
    get_equipment_stats,
    get_item_details,
    # 装备系统
    get_player_equipment,
    # 道具系统
    get_player_inventory,
    item_type_to_chinese,
    remove_item_from_inventory,
    unequip_item,
    update_equipment_stats,
    use_item,
)
from .monsters import initiate_monster_battle, show_monsters
from .utils import (
    RPG_HELP_TEXT,
    calculate_damage,
    calculate_exp_gain,
    get_exp_for_level,
    get_level_from_exp,
)

# 导出的函数和类
__all__ = [
    # 核心功能
    'rpg_command_handler',
    'setup_rpg_handlers',

    # 角色系统
    'get_character',
    'create_character',
    'update_character_stats',
    'get_user_id_by_username',
    'check_and_process_level_up',
    'set_battle_allowance',
    'heal_character',

    # 战斗系统
    'initiate_battle',
    'run_battle',
    'show_monsters',
    'initiate_monster_battle',

    # 计算功能
    'calculate_damage',
    'calculate_exp_gain',
    'get_exp_for_level',
    'get_level_from_exp',

    # 帮助信息
    'RPG_HELP_TEXT',

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
