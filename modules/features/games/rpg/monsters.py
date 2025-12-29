import logging
import time
from typing import Dict, Any, Optional
import asyncio

from telegram.constants import ParseMode

# 导入自定义模块
from core import process_user

from .utils import calculate_damage, get_level_from_exp
from .characters import check_and_process_level_up, get_character, update_character_stats

# 怪物数据字典，包含各种怪物的属性
MONSTERS = {
    "goblin": {
        "name": "哥布林",
        "level": 1,
        "hp": 8,
        "atk": 2,
        "def": 1,
        "exp_reward": 15,
        "coin_reward": 2,
        "description": "一个弱小但狡猾的生物，常在森林中出没。"
    },
    "wolf": {
        "name": "野狼",
        "level": 1,
        "hp": 5,
        "atk": 3,
        "def": 1,
        "exp_reward": 15,
        "coin_reward": 3,
        "description": "凶猛的野兽，群居生活，攻击力较强。"
    },
    "skeleton": {
        "name": "骷髅兵",
        "level": 2,
        "hp": 10,
        "atk": 3,
        "def": 2,
        "exp_reward": 25,
        "coin_reward": 4,
        "description": "被黑魔法复活的骸骨，手持生锈的武器。"
    }
    # 后续可以添加更多怪物
}

# 怪物战斗冷却时间（秒）
MONSTER_BATTLE_COOLDOWN = 300  # 5分钟冷却

# 用户的怪物战斗冷却记录 {user_id: last_battle_time}
monster_battle_cooldowns = {}

async def show_monsters(update, context):
    """显示所有怪物的信息"""
    if not MONSTERS:
        await update.message.reply_text("目前没有可挑战的怪物。")
        return
    
    monsters_info = "🎮 **可挑战的怪物列表** 🎮\n\n"
    for monster_id, monster in MONSTERS.items():
        monsters_info += f"**{monster['name']}** (ID: {monster_id})\n"
        monsters_info += f"等级: {monster['level']}\n"
        monsters_info += f"生命值: {monster['hp']}\n"
        monsters_info += f"攻击力: {monster['atk']}\n"
        monsters_info += f"防御力: {monster['def']}\n"
        monsters_info += f"经验奖励: {monster['exp_reward']}\n"
        monsters_info += f"金币奖励: {monster['coin_reward']}\n"
        monsters_info += f"描述: {monster['description']}\n\n"
    
    monsters_info += "使用 `/rpg battle monster <怪物ID>` 来挑战怪物。"
    
    await update.message.reply_text(monsters_info, parse_mode=ParseMode.MARKDOWN)

async def initiate_monster_battle(update, context, monster_id: str):
    """处理玩家与怪物的战斗"""
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    
    # 1. 检查怪物是否存在
    if monster_id not in MONSTERS:
        await update.message.reply_text(f"找不到ID为 '{monster_id}' 的怪物。使用 `/rpg monsters` 查看所有可挑战的怪物。")
        return
    
    monster = MONSTERS[monster_id]
    
    # 2. 检查冷却时间
    current_time = time.time()
    if user_id in monster_battle_cooldowns:
        last_battle_time = monster_battle_cooldowns[user_id]
        cooldown_remaining = last_battle_time + MONSTER_BATTLE_COOLDOWN - current_time
        
        if cooldown_remaining > 0:
            minutes, seconds = divmod(int(cooldown_remaining), 60)
            await update.message.reply_text(f"你需要休息一下！还需要等待 {minutes}分{seconds}秒 才能再次挑战怪物。")
            return
    
    # 3. 检查玩家角色是否存在
    character = await get_character(user_id)
    if not character:
        await update.message.reply_text("你还没有创建角色，请先使用 `/rpg` 命令创建。")
        return
    
    # 4. 检查角色生命值
    if character['hp'] <= 0:
        await update.message.reply_text("你的生命值过低，无法发起战斗！先使用 `/rpg heal` 恢复生命值。")
        return
    
    # 5. 开始战斗
    await update.message.reply_text(f"🏹 你向 **{monster['name']}** 发起了挑战！战斗开始...")
    
    # 创建怪物实例（复制怪物数据以免修改原始数据）
    monster_instance = monster.copy()
    
    # 战斗逻辑
    battle_log = [f"**{username}** vs **{monster['name']}**\n"]
    
    player_hp = character['hp']
    monster_hp = monster_instance['hp']
    
    # 玩家先攻
    current_attacker = "player"
    round_number = 1
    
    # 进行战斗回合，直到一方HP归零
    while player_hp > 0 and monster_hp > 0:
        battle_log.append(f"**回合 {round_number}:**")
        
        if current_attacker == "player":
            # 玩家攻击怪物
            damage = calculate_damage(character, {'def': monster_instance['def']})
            monster_hp = max(0, monster_hp - damage)
            battle_log.append(f"{username} 对 {monster_instance['name']} 造成了 {damage} 点伤害！")
            battle_log.append(f"{monster_instance['name']} 剩余HP: {monster_hp}")
            current_attacker = "monster"
        else:
            # 怪物攻击玩家
            damage = max(0, monster_instance['atk'] - character['def'])
            player_hp = max(0, player_hp - damage)
            battle_log.append(f"{monster_instance['name']} 对 {username} 造成了 {damage} 点伤害！")
            battle_log.append(f"{username} 剩余HP: {player_hp}")
            current_attacker = "player"
        
        round_number += 1
        # 防止战斗无限进行
        if round_number > 20:
            battle_log.append("战斗时间过长，以平局结束！")
            break
    
    # 战斗结果
    if player_hp <= 0 and monster_hp <= 0:
        battle_log.append("\n战斗结果: 平局！双方同归于尽。")
        result = "draw"
    elif player_hp <= 0:
        battle_log.append(f"\n战斗结果: 失败！你被 {monster_instance['name']} 击败了。")
        result = "lose"
    else:
        battle_log.append(f"\n战斗结果: 胜利！你击败了 {monster_instance['name']}。")
        result = "win"
    
    # 发送战斗日志
    battle_log_text = "\n".join(battle_log)
    await update.message.reply_text(battle_log_text, parse_mode=ParseMode.MARKDOWN)
    
    # 更新冷却时间
    monster_battle_cooldowns[user_id] = current_time
    
    # 处理战斗后果
    # 1. 更新玩家HP
    await update_character_stats(user_id, {'hp': player_hp})
    
    # 2. 如果玩家胜利，给予奖励
    if result == "win":
        # 经验奖励
        exp_reward = monster_instance['exp_reward']
        new_exp = character['experience'] + exp_reward
        await update_character_stats(user_id, {'experience': new_exp})
        
        # 金币奖励
        coin_reward = monster_instance['coin_reward']
        await process_user.async_update_user_coins(user_id, coin_reward)
        
        # 奖励消息
        reward_message = f"🎁 战斗奖励:\n获得 {exp_reward} 点经验值\n获得 {coin_reward} 枚金币"
        await update.message.reply_text(reward_message)
        
        # 检查升级
        await check_and_process_level_up(user_id, context)
    elif result == "lose":
        # 失败消息
        await update.message.reply_text("😢 战斗失败。使用 `/rpg heal` 恢复生命值，然后再尝试挑战吧！")
        
    return result 
