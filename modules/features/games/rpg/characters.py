import logging
import re
from typing import Optional, Dict, Any

from sqlalchemy.exc import IntegrityError

from core import mysql_connection, process_user

from .utils import get_level_from_exp

# --- 数据库交互函数 (RPG 角色) ---

async def get_character(user_id: int) -> Optional[Dict[str, Any]]:
    """异步获取用户角色数据"""
    try:
        row = await mysql_connection.fetch_one(
            "SELECT * FROM rpg_characters WHERE user_id = %s",
            (user_id,),
            mapping=True,
        )
        return dict(row) if row else None
    except Exception as e:
        logging.error(f"获取角色数据时出错 (用户ID: {user_id}): {e}")
        return None


async def create_character(user_id: int) -> bool:
    """异步为用户创建初始角色"""
    try:
        await mysql_connection.execute(
            """
            INSERT INTO rpg_characters (user_id, level, hp, max_hp, atk, matk, def, experience, allow_battle)
            VALUES (%s, 1, 10, 10, 2, 0, 1, 0, TRUE)
            """,
            (user_id,),
        )
        logging.info(f"为用户 {user_id} 创建了 RPG 角色")
        return True
    except IntegrityError:
        # 用户可能已经有角色了（例如并发创建），忽略错误
        logging.warning(f"尝试为用户 {user_id} 创建角色，但似乎已存在。")
        return True
    except Exception as e:
        logging.error(f"创建角色时出错 (用户ID: {user_id}): {e}")
        return False

# --- 获取用户ID通过用户名 ---
async def get_user_id_by_username(username: str) -> Optional[int]:
    """异步根据用户名获取用户ID (查询 user 表的 name 列)"""
    # 清理可能存在的 @ 符号
    clean_username = username.strip().lstrip('@')
    if not clean_username:
        return None

    try:
        result = await mysql_connection.fetch_one(
            "SELECT id FROM user WHERE name = %s",
            (clean_username,),
        )
        return result[0] if result else None
    except Exception as e:
        logging.error(f"通过用户名获取用户ID时出错 (用户名: {clean_username}): {e}")
        return None

# --- 更新角色数据 ---
async def update_character_stats(user_id: int, updates: dict) -> bool:
    """异步更新角色数据"""
    if not updates:
        return False # 没有要更新的内容

    # 构建 SET 子句和值列表
    set_parts = []
    values = []
    for key, value in updates.items():
        # 简单的验证，防止非法字段名 (更健壮的方法是预定义允许的字段)
        if re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', key):
            set_parts.append(f"{key} = %s")
            values.append(value)
        else:
            logging.warning(f"尝试更新非法字段名: {key}")
            return False # 阻止更新

    if not set_parts:
        return False # 没有有效的更新字段

    set_clause = ", ".join(set_parts)
    values.append(user_id) # 添加 user_id 到值的末尾用于 WHERE 子句

    try:
        async with mysql_connection.transaction() as connection:
            update_query = f"UPDATE rpg_characters SET {set_clause} WHERE user_id = %s"
            result = await connection.exec_driver_sql(update_query, tuple(values))
            rows_affected = result.rowcount
        logging.info(f"更新了用户 {user_id} 的角色数据: {updates}, 影响行数: {rows_affected}")
        return rows_affected > 0
    except Exception as e:
        logging.error(f"更新角色数据时出错 (用户ID: {user_id}, 更新: {updates}): {e}")
        return False

# --- 设置是否允许被挑战 ---
async def set_battle_allowance(update, context, allow: bool):
    
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name

    character = await get_character(user_id)
    if not character:
        await update.message.reply_text("你还没有创建角色，请先使用 `/rpg` 创建。")
        return

    success = await update_character_stats(user_id, {'allow_battle': allow})

    if success:
        status = "允许" if allow else "禁止"
        await update.message.reply_text(f"已将你的状态设置为 **{status}** 被挑战。")
        logging.info(f"用户 {user_id} ({username}) 设置 allow_battle 为 {allow}")
    else:
        await update.message.reply_text("更新设置失败，请稍后再试。")

# --- 升级逻辑 ---
async def check_and_process_level_up(user_id: int, context):
    """检查经验值是否足够升级，并处理升级逻辑，发送通知"""
    character = await get_character(user_id)
    if not character:
        return

    current_level = get_level_from_exp(character['experience'])

    # 检查数据库记录的等级是否需要更新（例如，如果之前因为某种原因没更新）
    if character['level'] != current_level:
        logging.info(f"用户 {user_id} 等级从 {character['level']} 修正为 {current_level}")
        updates = {'level': current_level}

        # --- 定义属性成长规则 --- 
        # 每次升级增加的属性值 (可以调整)
        hp_increase = 5
        atk_increase = 1
        matk_increase = 0 # 魔法攻击不成长
        def_increase = 1

        # 计算新属性 (基于升到的新等级 current_level)
        level_diff = current_level - character['level']
        if level_diff > 0:
             new_max_hp = character['max_hp'] + hp_increase * level_diff
             new_hp = new_max_hp # 升级时回满血
             new_atk = character['atk'] + atk_increase * level_diff
             new_matk = character['matk'] + matk_increase * level_diff # 保持不变
             new_def = character['def'] + def_increase * level_diff

             updates['max_hp'] = new_max_hp
             updates['hp'] = new_hp
             updates['atk'] = new_atk
             updates['matk'] = new_matk
             updates['def'] = new_def

             # 发送升级提示
             level_up_message = f"🎉 恭喜你升到了 {current_level} 级！\n"
             level_up_message += f"HP: +{hp_increase * level_diff} ({new_max_hp}), ATK: +{atk_increase * level_diff} ({new_atk}), DEF: +{def_increase * level_diff} ({new_def})"
             logging.info(f"用户 {user_id} 升级! 消息: {level_up_message}")
             try:
                 await context.bot.send_message(chat_id=user_id, text=level_up_message)
             except Exception as e:
                 logging.error(f"向用户 {user_id} 发送升级通知失败: {e}")

        # 更新数据库
        await update_character_stats(user_id, updates)

# 添加恢复HP的命令
async def heal_character(update, context):
    """处理 /rpg heal 命令，恢复角色HP"""
    user_id = update.effective_user.id
    
    # 获取角色信息
    character = await get_character(user_id)
    if not character:
        await update.message.reply_text("你还没有创建角色，请先使用 `/rpg` 命令创建角色。")
        return
    
    # 检查是否已满血
    if character['hp'] >= character['max_hp']:
        await update.message.reply_text("你的生命值已经是满的了！")
        return
        
    # 获取用户金币
    user_coins = await process_user.async_get_user_coins(user_id)
    heal_cost = 10  # 恢复费用
    
    if user_coins < heal_cost:
        await update.message.reply_text(f"恢复生命值需要 {heal_cost} 金币，但你只有 {user_coins} 金币。")
        return
    
    # 扣除金币并恢复HP
    await process_user.async_update_user_coins(user_id, -heal_cost)
    await update_character_stats(user_id, {'hp': character['max_hp']})
    
    await update.message.reply_text(f"花费 {heal_cost} 金币恢复了生命值！\n当前HP: {character['max_hp']}/{character['max_hp']}") 
