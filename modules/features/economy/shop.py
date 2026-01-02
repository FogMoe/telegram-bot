import asyncio
import random
from core import mysql_connection
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from datetime import datetime, date, timedelta
import time
from core.command_cooldown import cooldown

# 定义全局锁，确保购买过程的原子性
lock = asyncio.Lock()

# 添加用户刮刮乐记录字典，用于实现保底机制
# 格式: {user_id: {'count': 连续小于10金币次数, 'date': 最后抽取日期}}
scratch_records = {}

# 添加用户欢乐彩记录字典，用于实现保底机制
# 格式: {user_id: {'count': 连续0金币次数, 'date': 最后抽取日期}}
huanle_records = {}

# 添加用户最后抽奖消息记录
# 格式: {(user_id, chat_id): {'message_id': 消息ID, 'timestamp': 最后发送时间, 'message_type': '消息类型'}}
last_lottery_messages = {}

# 设置消息更新阈值（秒）- 超过这个时间才会发送新消息
MESSAGE_UPDATE_THRESHOLD = 30

@cooldown
async def shop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /shop 命令：发送商城一级菜单
    """
    keyboard = [
        [InlineKeyboardButton("购买权限", callback_data="shop_buy_permission")],
        [InlineKeyboardButton("购买记忆上限 +1 - 100金币", callback_data="shop_buy_memory_limit")],
        [InlineKeyboardButton("购买彩票", callback_data="shop_buy_lottery")],
        [InlineKeyboardButton("关闭商店", callback_data="shop_close")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("欢迎来到商城，请选择购买项目：", reply_markup=reply_markup)

async def shop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    处理商城按钮回调：
    - 一级菜单：显示“购买权限”、“购买记忆上限”、“购买彩票”和“关闭商店”按钮。
    - “购买权限”按钮：进入二级菜单，显示升级权限选项及返回按钮。
    - “购买彩票”按钮：进入二级菜单，显示“购买刮刮乐 - 10金币”、“购买欢乐彩 - 1金币”和“返回”按钮。
    - “购买刮刮乐 - 10金币”按钮：执行刮刮乐购买逻辑。
    - “购买欢乐彩 - 1金币”按钮：执行欢乐彩购买逻辑。
    - “返回”按钮：返回到一级菜单。
    - “关闭商店”按钮：删除商城消息。
    """
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = update.effective_chat.id

    if query.data == "shop_buy_permission":
        # 进入购买权限二级菜单
        keyboard = [
            [InlineKeyboardButton("升级权限等级到1级 - 50金币", callback_data="shop_upgrade_1")],
            [InlineKeyboardButton("升级权限等级到2级 - 100金币", callback_data="shop_upgrade_2")],
            [InlineKeyboardButton("返回", callback_data="shop_home")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        try:
            await query.edit_message_text("请选择购买的项目：", reply_markup=reply_markup)
        except Exception:
            pass

    elif query.data == "shop_buy_lottery":
        # 进入购买彩票二级菜单
        keyboard = [
            [InlineKeyboardButton("购买刮刮乐 - 10金币", callback_data="shop_scratch")],
            [InlineKeyboardButton("购买欢乐彩 - 1金币", callback_data="shop_huanle")],
            [InlineKeyboardButton("返回", callback_data="shop_home")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        try:
            await query.edit_message_text("请选择购彩项目：", reply_markup=reply_markup)
        except Exception:
            pass

    elif query.data == "shop_buy_memory_limit":
        # 购买永久记忆上限 +1
        async with lock:
            try:
                async with mysql_connection.transaction() as connection:
                    result = await mysql_connection.fetch_one(
                        "SELECT coins, permanent_records_limit FROM user WHERE id = %s",
                        (user_id,),
                        connection=connection,
                    )
                    if not result:
                        await query.answer("请先使用 /me 命令获取个人信息。", show_alert=True)
                        return

                    user_coins, current_limit = result
                    if user_coins < 100:
                        await query.answer("硬币不足，无法购买此商品。", show_alert=True)
                        return

                    await connection.exec_driver_sql(
                        "UPDATE user SET coins = coins - %s, "
                        "permanent_records_limit = permanent_records_limit + 1 "
                        "WHERE id = %s",
                        (100, user_id),
                    )
                    new_row = await mysql_connection.fetch_one(
                        "SELECT permanent_records_limit FROM user WHERE id = %s",
                        (user_id,),
                        connection=connection,
                    )
                    if new_row and new_row[0] is not None:
                        new_limit = new_row[0]
                    else:
                        base_limit = current_limit if current_limit is not None else 100
                        new_limit = base_limit + 1
                    await query.answer(
                        f"购买成功！永久记忆上限已提升至 {new_limit} 条。",
                        show_alert=True,
                    )
            except Exception:
                await query.answer("购买出现错误，请稍后再试。", show_alert=True)

    elif query.data == "shop_home":
        # 返回到一级菜单
        keyboard = [
            [InlineKeyboardButton("购买权限", callback_data="shop_buy_permission")],
            [InlineKeyboardButton("购买记忆上限 +1 - 100金币", callback_data="shop_buy_memory_limit")],
            [InlineKeyboardButton("购买彩票", callback_data="shop_buy_lottery")],
            [InlineKeyboardButton("关闭商店", callback_data="shop_close")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        try:
            await query.edit_message_text("欢迎来到商城，请选择购买项目：", reply_markup=reply_markup)
        except Exception:
            pass

    elif query.data == "shop_close":
        # 删除商城消息
        try:
            await query.delete_message()
        except Exception:
            pass

    elif query.data == "shop_upgrade_1":
        # 执行购买升级权限到1级的操作
        async with lock:
            try:
                async with mysql_connection.transaction() as connection:
                    result = await mysql_connection.fetch_one(
                        "SELECT permission, coins FROM user WHERE id = %s",
                        (user_id,),
                        connection=connection,
                    )
                    if not result:
                        await query.answer("请先使用 /me 命令获取个人信息。", show_alert=True)
                        return

                    user_permission, user_coins = result
                    if user_permission != 0:
                        await query.answer("您已经拥有权限或已升级。", show_alert=True)
                    elif user_coins < 50:
                        await query.answer("硬币不足，无法购买此商品。", show_alert=True)
                    else:
                        await connection.exec_driver_sql(
                            "UPDATE user SET coins = coins - %s, permission = %s WHERE id = %s",
                            (50, 1, user_id),
                        )
                        await query.answer("购买成功！您的权限已升级到1级。", show_alert=True)
            except Exception:
                await query.answer("购买出现错误，请稍后再试。", show_alert=True)
                
    elif query.data == "shop_upgrade_2":
        # 执行购买升级权限到2级的操作
        async with lock:
            try:
                async with mysql_connection.transaction() as connection:
                    result = await mysql_connection.fetch_one(
                        "SELECT permission, coins FROM user WHERE id = %s",
                        (user_id,),
                        connection=connection,
                    )
                    if not result:
                        await query.answer("请先使用 /me 命令获取个人信息。", show_alert=True)
                        return

                    user_permission, user_coins = result
                    if user_permission == 0:
                        await query.answer("您需要先升级到1级权限。", show_alert=True)
                    elif user_permission >= 2:
                        await query.answer("您已经拥有2级或更高权限。", show_alert=True)
                    elif user_coins < 100:
                        await query.answer("硬币不足，无法购买此商品。", show_alert=True)
                    else:
                        await connection.exec_driver_sql(
                            "UPDATE user SET coins = coins - %s, permission = %s WHERE id = %s",
                            (100, 2, user_id),
                        )
                        await query.answer("购买成功！您的权限已升级到2级。", show_alert=True)
            except Exception:
                await query.answer("购买出现错误，请稍后再试。", show_alert=True)

    elif query.data == "shop_scratch":
        # 购买刮刮乐：扣除10金币，随机获得0～20金币
        async with lock:
            try:
                async with mysql_connection.transaction() as connection:
                    result = await mysql_connection.fetch_one(
                        "SELECT coins FROM user WHERE id = %s",
                        (user_id,),
                        connection=connection,
                    )
                    if not result:
                        await query.answer("请先使用 /me 命令获取个人信息。", show_alert=True)
                        return

                    user_coins = result[0]
                    if user_coins < 10:
                        await query.answer(f"硬币不足，您当前只有 {user_coins} 个硬币。", show_alert=True)
                        return

                    reward = random.randint(0, 20)
                    new_coins = user_coins - 10 + reward
                    await connection.exec_driver_sql(
                        "UPDATE user SET coins = %s WHERE id = %s",
                        (new_coins, user_id),
                    )

                    today = date.today()
                    if user_id in scratch_records:
                        if scratch_records[user_id]['date'] == today:
                            if reward < 10:
                                scratch_records[user_id]['count'] += 1
                            else:
                                scratch_records[user_id]['count'] = 0
                        else:
                            scratch_records[user_id] = {'count': 1 if reward < 10 else 0, 'date': today}
                    else:
                        scratch_records[user_id] = {'count': 1 if reward < 10 else 0, 'date': today}

                    bonus_message = ""
                    if scratch_records[user_id]['count'] >= 5:
                        await connection.exec_driver_sql(
                            "UPDATE user SET coins = coins + %s WHERE id = %s",
                            (10, user_id),
                        )
                        scratch_records[user_id]['count'] = 0
                        bonus_message = "由于您连续5次都没抽到10个以上的金币，系统赠送您10个金币作为安慰！"

                # 弹出提示
                message = f"恭喜！您获得了 {reward} 个金币。"
                if bonus_message:
                    message += f"\n\n{bonus_message}"
                await query.answer(message, show_alert=True)

                # 发送通知消息到当前聊天（优化为可能更新现有消息）
                user_username = f"@{query.from_user.username}" if query.from_user.username else query.from_user.first_name
                msg_text = f"{user_username} 花费10金币购买了刮刮乐，获得了 {reward} 个金币。"
                if bonus_message:
                    msg_text += f"\n{bonus_message}"
                    
                # 获取当前时间
                current_time = time.time()
                message_key = (user_id, chat_id)
                
                # 检查是否应该更新现有消息或发送新消息
                if (message_key in last_lottery_messages and 
                    current_time - last_lottery_messages[message_key]['timestamp'] < MESSAGE_UPDATE_THRESHOLD and
                    last_lottery_messages[message_key]['message_type'] == 'lottery'):
                    
                    # 获取当前消息行数
                    old_text = last_lottery_messages[message_key].get('text', '')
                    lines = old_text.split('\n')
                    
                    # 如果行数已经达到6行或更多，发送新消息而不是更新
                    if len(lines) >= 6:
                        # 发送新消息开始新记录
                        new_text = f"📊 最近的彩票记录:\n{user_username}: 刮刮乐 → {reward}金币"
                        if bonus_message:
                            new_text += " (触发保底奖励10金币!)"
                        sent_msg = await context.bot.send_message(chat_id=chat_id, text=new_text)
                        last_lottery_messages[message_key] = {
                            'message_id': sent_msg.message_id,
                            'timestamp': current_time,
                            'message_type': 'lottery',
                            'text': new_text
                        }
                    else:
                        # 更新现有消息，行数未满6行
                        try:
                            # 添加新的抽奖记录
                            new_text = old_text + f"\n{user_username}: 刮刮乐 → {reward}金币"
                            if bonus_message:
                                new_text += " (触发保底奖励10金币!)"
                                
                            await context.bot.edit_message_text(
                                chat_id=chat_id,
                                message_id=last_lottery_messages[message_key]['message_id'],
                                text=new_text
                            )
                            # 更新记录的文本内容
                            last_lottery_messages[message_key]['text'] = new_text
                            last_lottery_messages[message_key]['timestamp'] = current_time
                        except Exception as e:
                            # 如果编辑失败，发送新消息
                            new_text = f"📊 最近的彩票记录:\n{user_username}: 刮刮乐 → {reward}金币"
                            if bonus_message:
                                new_text += " (触发保底奖励10金币!)"
                            sent_msg = await context.bot.send_message(chat_id=chat_id, text=new_text)
                            last_lottery_messages[message_key] = {
                                'message_id': sent_msg.message_id,
                                'timestamp': current_time,
                                'message_type': 'lottery',
                                'text': new_text
                            }
                else:
                    # 发送新消息
                    new_text = f"📊 最近的彩票记录:\n{user_username}: 刮刮乐 → {reward}金币"
                    if bonus_message:
                        new_text += " (触发保底奖励10金币!)"
                    sent_msg = await context.bot.send_message(chat_id=chat_id, text=new_text)
                    last_lottery_messages[message_key] = {
                        'message_id': sent_msg.message_id,
                        'timestamp': current_time,
                        'message_type': 'lottery',
                        'text': new_text
                    }
            except Exception as e:
                await query.answer(f"购买刮刮乐时出错：{str(e)}", show_alert=True)

    elif query.data == "shop_huanle":
        # 购买欢乐彩：扣除1金币，根据概率获得奖励
        async with lock:
            try:
                async with mysql_connection.transaction() as connection:
                    result = await mysql_connection.fetch_one(
                        "SELECT coins FROM user WHERE id = %s",
                        (user_id,),
                        connection=connection,
                    )
                    if not result:
                        await query.answer("请先使用 /me 命令获取个人信息。", show_alert=True)
                        return

                    user_coins = result[0]
                    if user_coins < 1:
                        await query.answer(f"硬币不足，您当前只有 {user_coins} 个硬币。", show_alert=True)
                        return

                    # 扣除1金币并根据概率获得奖励：
                    # 0金币：80% ； 1金币：19% ； 5金币：0.95% ； 100金币：0.05%
                    p = random.random()
                    if p < 0.80:
                        reward = 0
                    elif p < 0.80 + 0.19:
                        reward = 1
                    elif p < 0.80 + 0.19 + 0.0095:
                        reward = 5
                    else:
                        reward = 100

                    new_coins = user_coins - 1 + reward
                    await connection.exec_driver_sql(
                        "UPDATE user SET coins = %s WHERE id = %s",
                        (new_coins, user_id),
                    )

                    today = date.today()
                    if user_id in huanle_records:
                        if huanle_records[user_id]['date'] == today:
                            if reward == 0:
                                huanle_records[user_id]['count'] += 1
                            else:
                                huanle_records[user_id]['count'] = 0
                        else:
                            huanle_records[user_id] = {'count': 1 if reward == 0 else 0, 'date': today}
                    else:
                        huanle_records[user_id] = {'count': 1 if reward == 0 else 0, 'date': today}

                    bonus_message = ""
                    if huanle_records[user_id]['count'] >= 5:
                        await connection.exec_driver_sql(
                            "UPDATE user SET coins = coins + %s WHERE id = %s",
                            (2, user_id),
                        )
                        huanle_records[user_id]['count'] = 0
                        bonus_message = "由于您连续5次都没有获得奖励，系统赠送您2个金币作为安慰！"

                # 弹出提示
                message = f"恭喜！您获得了 {reward} 个金币。"
                if bonus_message:
                    message += f"\n\n{bonus_message}"
                await query.answer(message, show_alert=True)

                # 发送通知消息到当前聊天（优化为可能更新现有消息）
                user_username = f"@{query.from_user.username}" if query.from_user.username else query.from_user.first_name
                
                # 获取当前时间
                current_time = time.time()
                message_key = (user_id, chat_id)
                
                # 检查是否应该更新现有消息或发送新消息
                if (message_key in last_lottery_messages and 
                    current_time - last_lottery_messages[message_key]['timestamp'] < MESSAGE_UPDATE_THRESHOLD and
                    last_lottery_messages[message_key]['message_type'] == 'lottery'):
                    
                    # 获取当前消息行数
                    old_text = last_lottery_messages[message_key].get('text', '')
                    lines = old_text.split('\n')
                    
                    # 如果行数已经达到6行或更多，发送新消息而不是更新
                    if len(lines) >= 6:
                        # 发送新消息开始新记录
                        new_text = f"📊 最近的彩票记录:\n{user_username}: 欢乐彩 → {reward}金币"
                        if bonus_message:
                            new_text += " (触发保底奖励2金币!)"
                        sent_msg = await context.bot.send_message(chat_id=chat_id, text=new_text)
                        last_lottery_messages[message_key] = {
                            'message_id': sent_msg.message_id,
                            'timestamp': current_time,
                            'message_type': 'lottery',
                            'text': new_text
                        }
                    else:
                        # 更新现有消息，行数未满6行
                        try:
                            # 添加新的抽奖记录
                            new_text = old_text + f"\n{user_username}: 欢乐彩 → {reward}金币"
                            if bonus_message:
                                new_text += " (触发保底奖励2金币!)"
                                
                            await context.bot.edit_message_text(
                                chat_id=chat_id,
                                message_id=last_lottery_messages[message_key]['message_id'],
                                text=new_text
                            )
                            # 更新记录的文本内容
                            last_lottery_messages[message_key]['text'] = new_text
                            last_lottery_messages[message_key]['timestamp'] = current_time
                        except Exception as e:
                            # 如果编辑失败，发送新消息
                            new_text = f"📊 最近的彩票记录:\n{user_username}: 欢乐彩 → {reward}金币"
                            if bonus_message:
                                new_text += " (触发保底奖励2金币!)"
                            sent_msg = await context.bot.send_message(chat_id=chat_id, text=new_text)
                            last_lottery_messages[message_key] = {
                                'message_id': sent_msg.message_id,
                                'timestamp': current_time,
                                'message_type': 'lottery',
                                'text': new_text
                            }
                else:
                    # 发送新消息
                    new_text = f"📊 最近的彩票记录:\n{user_username}: 欢乐彩 → {reward}金币"
                    if bonus_message:
                        new_text += " (触发保底奖励2金币!)"
                    sent_msg = await context.bot.send_message(chat_id=chat_id, text=new_text)
                    last_lottery_messages[message_key] = {
                        'message_id': sent_msg.message_id,
                        'timestamp': current_time,
                        'message_type': 'lottery',
                        'text': new_text
                    }
            except Exception as e:
                await query.answer("购买欢乐彩时出错，请稍后再试。", show_alert=True)

# 修改清理函数以适配JobQueue使用
async def cleanup_message_records_job(context: ContextTypes.DEFAULT_TYPE):
    """清理旧的消息记录，每小时运行一次"""
    current_time = time.time()
    # 删除超过1小时的记录
    expired_keys = [k for k, v in last_lottery_messages.items() 
                  if current_time - v['timestamp'] > 3600]
    for key in expired_keys:
        if key in last_lottery_messages:
            del last_lottery_messages[key]
    print(f"清理了{len(expired_keys)}条过期抽奖消息记录")

# 保留原始函数以保持兼容性
async def cleanup_message_records():
    """原始清理函数，现在直接调用一次清理作业"""
    await cleanup_message_records_job(None)
