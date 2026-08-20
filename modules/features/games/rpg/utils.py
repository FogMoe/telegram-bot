import logging
import math
from concurrent.futures import ThreadPoolExecutor

# 创建线程池执行器用于异步数据库操作
rpg_db_executor = ThreadPoolExecutor(max_workers=5)

# --- 经验值与等级计算 ---
def get_exp_for_level(level: int) -> int:
    """计算升到下一级所需的总经验值 (示例公式) - 返回该等级的总经验上限"""
    if level <= 0:
        return 0
    # 例如: level 1 需要 100 exp, level 2 需要 300 exp, level 3 需要 600 exp ...
    return 50 * (level ** 2) + 50 * level

def get_level_from_exp(exp: int) -> int:
    """根据总经验值计算当前等级"""
    if exp < 0:
        return 1  # 经验不能为负
    
    level = 1
    while True:
        # 如果经验值小于下一级所需经验，则当前等级为level
        if exp < get_exp_for_level(level):
            return level
        
        level += 1
        # 安全检查，防止无限循环
        if level > 1000:  # 假设最高级别为1000级
            logging.error(f"计算等级时出现异常高值: 经验={exp}")
            return 1000

# --- 经验值计算 ---
def calculate_exp_gain(winner_level: int, loser_level: int) -> int:
    """根据等级差计算经验值奖励"""
    level_diff = loser_level - winner_level

    # 基础经验值 (可以调整)
    base_exp = 50

    # 等级差影响因子 (可以调整)
    # 领先越多，经验越少；落后越多，经验越多
    if level_diff >= 10: # 落后10级及以上，经验最大化 (例如基础值的 2 倍)
        multiplier = 2.0
    elif level_diff <= -10: # 领先10级及以上，经验最小化 (例如基础值的 0.1 倍)
        multiplier = 0.1
    else:
        # 在 -9 到 9 级之间线性插值或分段处理
        # 修正后的线性插值，确保在-10到10之间从0.1到2.0变化
        multiplier = 1.05 + (level_diff / 10) * 0.95 # 从 0.1 (diff=-10) 到 2.0 (diff=10)
        multiplier = max(0.1, min(2.0, multiplier)) # 限制在 0.1 和 2.0 之间

    # 计算最终经验值，向下取整
    exp_gain = math.floor(base_exp * multiplier)
    return max(1, exp_gain) # 保证至少获得 1 点经验

# --- 伤害计算 ---
def calculate_damage(attacker_stats: dict, defender_stats: dict, attack_type: str = 'physical') -> float:
    """计算单次攻击造成的伤害"""
    if attack_type == 'physical':
        damage = attacker_stats['atk'] - defender_stats['def']
    elif attack_type == 'magical':
        damage = attacker_stats['matk'] - (defender_stats['def'] / 2)
    else:
        damage = 0 # 未知攻击类型

    # 确保伤害至少为 0 (或根据规则设定最低伤害，例如 1)
    final_damage = max(0, damage)
    # 保留一位小数
    return round(final_damage, 1)

# --- RPG 帮助文本 ---
RPG_HELP_TEXT = """
**🎮 RPG游戏系统命令**

**基础命令:**
`/rpg` - 查看角色状态
`/rpg help` - 显示此帮助信息

**战斗系统:**
`/rpg battle <用户名>` - 与其他玩家战斗(每小时限制1次)
`/rpg battle monster <怪物ID>` - 与怪物战斗
`/rpg battle on|off` - 开启/关闭被挑战功能
`/rpg monsters` - 查看可挑战的怪物列表
`/rpg heal` - 恢复生命值

**装备系统:**
`/rpg equip` - 查看当前装备
`/rpg equip <装备ID>` - 装备指定物品
`/rpg equip unequip <类型>` - 卸下指定类型装备

**道具系统:**
`/rpg item` - 查看道具栏
`/rpg item <道具ID>` - 查看道具详情
`/rpg item use <道具ID>` - 使用道具

**商店系统:**
`/rpg shop` - 查看商店
`/rpg shop buy <物品ID>` - 购买物品

---
每场战斗后，您需要恢复生命值才能再次挑战。
与怪物战斗有5分钟冷却时间。
与玩家战斗胜利可获得对方部分金币和经验值。
击败怪物可获得固定的金币和经验奖励。
""" 