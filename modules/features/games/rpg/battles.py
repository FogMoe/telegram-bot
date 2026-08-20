import logging
import asyncio
import math
import time

# 导入自定义模块
from core import process_user

from .utils import calculate_damage, calculate_exp_gain, get_level_from_exp
from .characters import (
    check_and_process_level_up,
    get_character,
    get_user_id_by_username,
    update_character_stats,
)

# --- 玩家间战斗系统 ---
# 玩家战斗冷却时间（秒）
PLAYER_BATTLE_COOLDOWN = 3600  # 1小时冷却

# 用户的玩家战斗冷却记录 {user_id: last_battle_time}
player_battle_cooldowns = {}

async def run_battle(update, context, attacker_id: int, defender_id: int):
    """执行完整的战斗流程"""
    attacker_char = await get_character(attacker_id)
    defender_char = await get_character(defender_id)
    attacker_user = await context.bot.get_chat(attacker_id) # 获取用户信息用于显示名字
    defender_user = await context.bot.get_chat(defender_id)
    attacker_name = attacker_user.username or attacker_user.first_name
    defender_name = defender_user.username or defender_user.first_name

    if not attacker_char or not defender_char:
        await update.message.reply_text("无法获取战斗双方的角色信息。")
        return

    # 初始化战斗状态
    attacker_hp = attacker_char['hp']
    defender_hp = defender_char['hp']
    battle_log = [f"战斗开始！ {defender_name} vs {attacker_name}"]

    # 确定先手 (被挑战者先攻)
    current_turn_id = defender_id
    turn_counter = 0
    max_turns = 20 # 防止无限循环

    while attacker_hp > 0 and defender_hp > 0 and turn_counter < max_turns:
        turn_counter += 1
        turn_log = f"\n**回合 {turn_counter}:**\n"

        if current_turn_id == defender_id:
            # 被挑战者攻击
            attacker_stats = defender_char
            defender_stats = attacker_char
            attack_name = defender_name
            defend_name = attacker_name
            target_hp = attacker_hp # 被挑战者攻击挑战者，所以目标是挑战者的HP
        else:
            # 挑战者攻击
            attacker_stats = attacker_char
            defender_stats = defender_char
            attack_name = attacker_name
            defend_name = defender_name
            target_hp = defender_hp # 挑战者攻击被挑战者，所以目标是被挑战者的HP

        # 目前只进行物理攻击，未来可扩展
        attack_type = 'physical'
        damage = calculate_damage(attacker_stats, defender_stats, attack_type)

        turn_log += f"{attack_name} 使用普通攻击对 {defend_name} "

        # 更新目标HP并记录日志
        target_hp -= damage
        target_hp = round(max(0, target_hp), 1) # 保持一位小数且不少于0
        
        # 根据当前回合更新正确的HP变量
        if current_turn_id == defender_id:
            attacker_hp = target_hp
        else:
            defender_hp = target_hp
            
        turn_log += f"造成了 {damage} 点伤害。 {defend_name} 剩余 HP: {target_hp}"
        battle_log.append(turn_log)

        # 检查战斗是否结束
        if attacker_hp <= 0 or defender_hp <= 0:
            break

        # 切换回合
        current_turn_id = attacker_id if current_turn_id == defender_id else defender_id

        await asyncio.sleep(0.5) # 轻微暂停，避免刷屏但不影响体验

    # --- 战斗结束处理 ---
    winner_id = None
    loser_id = None
    if attacker_hp <= 0 and defender_hp <= 0:
        battle_log.append("\n**战斗结果：平局！** (双方同时倒下)")
        # 平局也可能需要处理，例如双方都不获得/失去东西，或者都少量损失
    elif defender_hp <= 0:
        battle_log.append(f"\n**战斗结果：{attacker_name} 获胜！**")
        winner_id = attacker_id
        loser_id = defender_id
    elif attacker_hp <= 0:
        battle_log.append(f"\n**战斗结果：{defender_name} 获胜！**")
        winner_id = defender_id
        loser_id = attacker_id
    elif turn_counter >= max_turns:
         battle_log.append("\n**战斗结果：平局！** (超过最大回合数)")

    # 发送战斗日志
    # 为了避免消息过长，可以分段发送或只显示最后几回合
    full_log = "".join(battle_log)
    if len(full_log) > 4000: # Telegram 消息长度限制约为 4096
        await update.message.reply_text("战斗日志过长，仅显示部分：\n..." + full_log[-3500:], parse_mode='Markdown')
    else:
        await update.message.reply_text(full_log, parse_mode='Markdown')

    # --- 奖励与惩罚处理 ---
    if winner_id and loser_id:
        winner_char = await get_character(winner_id)
        loser_char = await get_character(loser_id)
        winner_user = await context.bot.get_chat(winner_id)
        loser_user = await context.bot.get_chat(loser_id)
        winner_name = winner_user.username or winner_user.first_name
        loser_name = loser_user.username or loser_user.first_name

        # 1. 计算金币变化
        loser_coins = await process_user.async_get_user_coins(loser_id)
        coins_lost = math.floor(loser_coins * 0.10)
        coins_to_winner = math.floor(coins_lost * 0.8) # 80% 给赢家
        coins_deducted = coins_lost # 实际扣除额

        reward_log = f"\n--- 战后结算 ---\n{loser_name} 损失了 {coins_deducted} 🪙 金币。\n"
        reward_log += f"{winner_name} 获得了 {coins_to_winner} 🪙 金币。\n"

        # 更新金币
        await process_user.async_update_user_coins(loser_id, -coins_deducted)
        await process_user.async_update_user_coins(winner_id, coins_to_winner)

        # 2. 计算经验值变化
        winner_level = get_level_from_exp(winner_char['experience'])
        loser_level = get_level_from_exp(loser_char['experience'])
        exp_gain = calculate_exp_gain(winner_level, loser_level)

        reward_log += f"{winner_name} 获得了 {exp_gain} 点经验值！"

        # 更新获胜者经验值和血量
        new_exp = winner_char['experience'] + exp_gain
        await update_character_stats(winner_id, {'experience': new_exp})
        
        # 更新双方的HP到数据库
        await update_character_stats(attacker_id, {'hp': attacker_hp})
        await update_character_stats(defender_id, {'hp': defender_hp})

        await update.message.reply_text(reward_log)

        # 3. 检查获胜者是否升级
        try:
            await check_and_process_level_up(winner_id, context)
        except Exception as e:
            logging.error(f"处理升级时出错: {e}")

    # 返回战斗结果（胜者ID和负者ID，如果是平局则都为None）
    return winner_id, loser_id

# --- 发起战斗 --- 
async def initiate_battle(update, context, target_username: str):
    """处理 /rpg battle <用户名> 命令"""
    attacker_id = update.effective_user.id

    # 1. 检查发起者是否有角色
    attacker_char = await get_character(attacker_id)
    if not attacker_char:
        await update.message.reply_text("你还没有创建角色，请先使用 `/rpg` 创建。")
        return
    if attacker_char['hp'] <= 0:
         await update.message.reply_text("你当前生命值过低，无法发起战斗！使用 `/rpg heal` 恢复生命值。")
         return
         
    # 1.5 检查冷却时间
    current_time = time.time()
    if attacker_id in player_battle_cooldowns:
        last_battle_time = player_battle_cooldowns[attacker_id]
        cooldown_remaining = last_battle_time + PLAYER_BATTLE_COOLDOWN - current_time
        
        if cooldown_remaining > 0:
            minutes, seconds = divmod(int(cooldown_remaining), 60)
            hours, minutes = divmod(minutes, 60)
            if hours > 0:
                await update.message.reply_text(f"你需要休息一下！还需要等待 {hours}小时{minutes}分钟 才能再次挑战其他玩家。")
            else:
                await update.message.reply_text(f"你需要休息一下！还需要等待 {minutes}分{seconds}秒 才能再次挑战其他玩家。")
            return

    # 2. 查找目标用户 ID
    target_id = await get_user_id_by_username(target_username)
    if not target_id:
        await update.message.reply_text(f"找不到用户名为 '{target_username}' 的玩家。请确保输入正确（区分大小写，不含@）。")
        return

    # 3. 不能挑战自己
    if attacker_id == target_id:
        await update.message.reply_text("你不能挑战自己！")
        return

    # 4. 检查目标用户是否有角色
    defender_char = await get_character(target_id)
    if not defender_char:
        target_user = await context.bot.get_chat(target_id)
        target_display_name = target_user.username or target_user.first_name
        await update.message.reply_text(f"玩家 {target_display_name} 还没有创建 RPG 角色。")
        return

    # 5. 检查目标用户是否允许被挑战
    if not defender_char['allow_battle']:
        target_user = await context.bot.get_chat(target_id)
        target_display_name = target_user.username or target_user.first_name
        await update.message.reply_text(f"玩家 {target_display_name} 当前设置了不允许被挑战。")
        return
        
    if defender_char['hp'] <= 0:
         target_user = await context.bot.get_chat(target_id)
         target_display_name = target_user.username or target_user.first_name
         await update.message.reply_text(f"玩家 {target_display_name} 当前生命值过低，无法接受挑战！")
         return

    # 6. (可选) 添加其他战斗限制，例如等级差距过大等

    # --- 开始战斗 --- 
    target_user = await context.bot.get_chat(target_id)
    target_display_name = target_user.username or target_user.first_name
    await update.message.reply_text(f"正在向 {target_display_name} 发起挑战...⚔️")

    # 更新冷却时间
    player_battle_cooldowns[attacker_id] = current_time

    # 调用战斗执行函数
    return await run_battle(update, context, attacker_id, target_id) 
