import logging
from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from core.command_cooldown import cooldown

# 导入自定义模块
from .utils import get_exp_for_level, get_level_from_exp, RPG_HELP_TEXT
from .characters import get_character, create_character, set_battle_allowance, heal_character
from .battles import initiate_battle
from .monsters import show_monsters, initiate_monster_battle
from .equipment import (
    get_player_equipment, 
    equip_item, 
    unequip_item, 
    get_equipment_details,
    equipment_type_to_chinese,
    get_player_inventory,
    get_item_details,
    add_item_to_inventory,
    remove_item_from_inventory,
    use_item,
    item_type_to_chinese,
    INVENTORY_CAPACITY
)
from core import process_user

# --- 主命令处理 ---
@cooldown  # 应用命令冷却
async def rpg_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /rpg 命令及其子命令"""
    user = update.effective_user
    if not user:
        return

    user_id = user.id
    username = user.username or user.first_name
    args = context.args

    # --- 处理子命令 --- 
    if args:
        command = args[0].lower()
        
        # 帮助命令
        if command == "help":
            await update.message.reply_text(RPG_HELP_TEXT, parse_mode=ParseMode.MARKDOWN)
            return
        
        # 怪物列表命令
        elif command == "monsters":
            await show_monsters(update, context)
            return
            
        # 战斗命令
        elif command == "battle":
            if len(args) > 1:
                if args[1].lower() == "monster" and len(args) > 2:
                    # 怪物战斗
                    monster_id = args[2].lower()
                    await initiate_monster_battle(update, context, monster_id)
                    return
                elif args[1].lower() in ["on", "off"]:
                    # 设置允许被挑战
                    allow = args[1].lower() == "on"
                    await set_battle_allowance(update, context, allow)
                    return
                else:
                    # 玩家对战
                    target_username = args[1]
                    await initiate_battle(update, context, target_username)
                    return
            else:
                await update.message.reply_text(
                    "用法:\n"
                    "`/rpg battle <用户名>` - 与玩家对战\n"
                    "`/rpg battle monster <怪物ID>` - 与怪物对战\n"
                    "`/rpg battle on|off` - 开启/关闭被挑战功能"
                , parse_mode=ParseMode.MARKDOWN)
                return
                
        # 治疗命令
        elif command == "heal":
            await heal_character(update, context)
            return

        # 装备命令
        elif command == "equipment" or command == "equip":
            await handle_equipment_command(update, context)
            return
            
        # 道具命令 
        elif command == "inventory" or command == "item":
            await handle_inventory_command(update, context)
            return

        # 如果是未知命令，显示帮助
        else:
            await update.message.reply_text(f"未知命令: {command}\n请使用 `/rpg help` 查看可用命令。", parse_mode=ParseMode.MARKDOWN)
            return

    # --- 默认行为: 显示角色状态 --- 
    character_data = await get_character(user_id)

    if not character_data:
        # 2.1 如果没有角色，尝试创建角色
        # 首先确保用户在主用户表中存在，否则外键约束会失败
        if not await process_user.async_user_exists(user_id):
             # 如果主用户不存在，可能需要提示用户先用 /me 或其他命令注册
             await update.message.reply_text("需要先在系统中记录你的信息，请尝试使用 `/me` 命令。", parse_mode=ParseMode.MARKDOWN)
             logging.info(f"用户 {user_id} ({username}) 尝试使用 /rpg 但主用户记录不存在。")
             return

        logging.info(f"用户 {user_id} ({username}) 没有RPG角色，尝试创建...")
        success = await create_character(user_id)
        if success:
            character_data = await get_character(user_id) # 重新获取数据
            if not character_data: # 如果重新获取失败
                 logging.error(f"成功创建角色后无法立即获取用户 {user_id} 的数据。")
                 await update.message.reply_text("创建角色后检索数据时发生错误，请稍后再试。")
                 return

            # 显示初始信息
            current_level = get_level_from_exp(character_data['experience'])
            exp_next_level = get_exp_for_level(current_level)
            exp_prev_level = get_exp_for_level(current_level - 1)
            exp_current_in_level = character_data['experience'] - exp_prev_level
            exp_needed_for_level = exp_next_level - exp_prev_level

            await update.message.reply_text(
                f"🎮 欢迎你，**{username}**！冒险者角色已创建。\n\n"
                f"**📊 角色状态**\n"
                f"等级: {current_level} (经验: {exp_current_in_level}/{exp_needed_for_level})\n"
                f"❤️ 生命值: {character_data['hp']}/{character_data['max_hp']}\n"
                f"⚔️ 攻击力: {character_data['atk']} | 🔮 魔法攻击: {character_data['matk']}\n"
                f"🛡️ 防御力: {character_data['def']}\n"
                f"🪙 金币: {await process_user.async_get_user_coins(user_id)}\n"
                f"🤺 允许被挑战: {'✅' if character_data['allow_battle'] else '❌'}\n\n"
                f"**📝 游戏指令**\n"
                f"使用 `/rpg help` 查看所有可用指令！"
            , parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text("创建角色失败，可能是数据库错误，请联系管理员。")
    else:
        # 2.2 如果有角色，显示当前状态
        current_level = get_level_from_exp(character_data['experience'])
        
        exp_next_level = get_exp_for_level(current_level)
        exp_prev_level = get_exp_for_level(current_level - 1)
        exp_current_in_level = character_data['experience'] - exp_prev_level
        exp_needed_for_level = exp_next_level - exp_prev_level

        # 获取最新金币数
        current_coins = await process_user.async_get_user_coins(user_id)

        await update.message.reply_text(
            f"🎮 冒险者 **{username}**，你的状态如下：\n\n"
            f"**📊 角色状态**\n"
            f"等级: {current_level} (经验: {exp_current_in_level}/{exp_needed_for_level})\n"
            f"❤️ 生命值: {character_data['hp']}/{character_data['max_hp']}\n"
            f"⚔️ 攻击力: {character_data['atk']} | 🔮 魔法攻击: {character_data['matk']}\n"
            f"🛡️ 防御力: {character_data['def']}\n"
            f"🪙 金币: {current_coins}\n"
            f"🤺 允许被挑战: {'✅' if character_data['allow_battle'] else '❌'}\n\n"
            f"**📝 常用指令**\n"
            f"`/rpg help` - 查看所有指令"
        ,parse_mode=ParseMode.MARKDOWN) 

# --- 装备系统命令处理 ---
async def handle_equipment_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理装备相关命令"""
    user = update.effective_user
    user_id = user.id
    args = context.args[1:] if len(context.args) > 1 else []
    
    # 检查玩家是否有角色
    character = await get_character(user_id)
    if not character:
        await update.message.reply_text("你还没有创建角色，请先使用 `/rpg` 创建。", parse_mode=ParseMode.MARKDOWN)
        return
        
    # 无参数时显示当前装备状态
    if not args:
        equipment = await get_player_equipment(user_id)
        if not equipment:
            await update.message.reply_text("获取装备信息失败，请稍后再试。")
            return
            
        # 构建装备信息文本
        equipped_text = []
        for slot in ['weapon', 'offhand', 'armor', 'treasure1', 'treasure2']:
            slot_name = equipment_type_to_chinese(slot)
            item_id = equipment[f"{slot}_id"]
            item_name = equipment[f"{slot}_name"] or "无"
            equipped_text.append(f"{slot_name}: {item_name}")
            
        message = (
            f"**📦 {user.username or user.first_name} 的装备**\n\n" +
            "\n".join(equipped_text) +
            "\n\n使用 `/rpg equip [装备ID]` 装备物品\n" +
            "使用 `/rpg equip unequip [类型]` 卸下装备"
        )
        await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)
        return
        
    # 装备物品
    if len(args) == 1 and args[0].isdigit():
        equipment_id = int(args[0])
        success, message = await equip_item(user_id, equipment_id)
        await update.message.reply_text(message)
        return
        
    # 卸下装备
    if len(args) >= 1 and args[0] == "unequip":
        if len(args) < 2:
            await update.message.reply_text(
                "请指定要卸下的装备类型：\n" +
                "`/rpg equip unequip weapon` - 卸下武器\n" +
                "`/rpg equip unequip offhand` - 卸下副手\n" +
                "`/rpg equip unequip armor` - 卸下护甲\n" +
                "`/rpg equip unequip treasure1` - 卸下宝物1\n" +
                "`/rpg equip unequip treasure2` - 卸下宝物2"
            , parse_mode=ParseMode.MARKDOWN)
            return
            
        equipment_type = args[1].lower()
        if equipment_type not in ['weapon', 'offhand', 'armor', 'treasure1', 'treasure2']:
            await update.message.reply_text(f"不支持的装备类型: {equipment_type}")
            return
            
        success, message = await unequip_item(user_id, equipment_type)
        await update.message.reply_text(message)
        return
        
    # 未识别的装备命令
    await update.message.reply_text(
        "装备命令用法：\n" +
        "`/rpg equip` - 查看当前装备\n" +
        "`/rpg equip [装备ID]` - 装备指定物品\n" +
        "`/rpg equip unequip [类型]` - 卸下指定类型装备"
    , parse_mode=ParseMode.MARKDOWN)

# --- 道具系统命令处理 ---
async def handle_inventory_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理道具相关命令"""
    user = update.effective_user
    user_id = user.id
    args = context.args[1:] if len(context.args) > 1 else []
    
    # 检查玩家是否有角色
    character = await get_character(user_id)
    if not character:
        await update.message.reply_text("你还没有创建角色，请先使用 `/rpg` 创建。", parse_mode=ParseMode.MARKDOWN)
        return
        
    # 无参数时显示当前道具
    if not args:
        inventory = await get_player_inventory(user_id)
        
        if not inventory:
            await update.message.reply_text(
                f"**🎒 {user.username or user.first_name} 的道具栏 (0/{INVENTORY_CAPACITY})**\n\n" +
                "道具栏空空如也..."
            , parse_mode=ParseMode.MARKDOWN)
            return
            
        # 构建道具信息文本
        items_text = []
        for item in inventory:
            item_type = item_type_to_chinese(item['type'])
            items_text.append(f"[{item['id']}] {item['name']} x{item['quantity']} ({item_type})")
            
        message = (
            f"**🎒 {user.username or user.first_name} 的道具栏 ({len(inventory)}/{INVENTORY_CAPACITY})**\n\n" +
            "\n".join(items_text) +
            "\n\n使用 `/rpg item use [道具ID]` 使用道具"
        )
        await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)
        return
        
    # 使用道具
    if len(args) >= 2 and args[0] == "use":
        if not args[1].isdigit():
            await update.message.reply_text("道具ID必须是数字。")
            return
            
        item_id = int(args[1])
        success, message = await use_item(user_id, item_id)
        await update.message.reply_text(message)
        return
        
    # 查看道具详情
    if len(args) >= 1 and args[0].isdigit():
        item_id = int(args[0])
        item = await get_item_details(item_id)
        
        if not item:
            await update.message.reply_text(f"找不到ID为 {item_id} 的道具。")
            return
            
        message = (
            f"**🔍 道具详情: {item['name']}**\n\n" +
            f"类型: {item_type_to_chinese(item['type'])}\n" +
            f"描述: {item['description']}\n" +
            f"效果: {item['effect']}\n" +
            f"价值: {item['price']} 金币\n\n" +
            "使用 `/rpg item use [道具ID]` 使用此道具"
        )
        await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)
        return
        
    # 未识别的道具命令
    await update.message.reply_text(
        "道具命令用法：\n" +
        "`/rpg item` - 查看道具栏\n" +
        "`/rpg item [道具ID]` - 查看道具详情\n" +
        "`/rpg item use [道具ID]` - 使用道具"
    , parse_mode=ParseMode.MARKDOWN) 
