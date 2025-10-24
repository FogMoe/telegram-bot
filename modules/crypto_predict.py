import asyncio
import mysql_connection
import process_user
import logging
from datetime import datetime, timedelta
from binance.um_futures import UMFutures
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CommandHandler, CallbackQueryHandler
from telegram.constants import ParseMode
import time
from command_cooldown import cooldown

# 用户级别的锁，而非全局锁，避免不同用户操作互相阻塞
user_locks = {}
# 每个用户的预测任务，防止重复开启
active_predict_tasks = {}  # {user_id: asyncio.Task}
# 添加一个全局字典来跟踪用户的按钮点击，防止重复点击
button_click_cooldown = {}  # {user_id: last_click_time}
CLICK_COOLDOWN_SECONDS = 3  # 设置按钮冷却时间为3秒

async def get_user_lock(user_id):
    """获取特定用户的锁，如果不存在则创建"""
    if (user_id not in user_locks):
        user_locks[user_id] = asyncio.Lock()
    return user_locks[user_id]

async def get_btc_price():
    """获取比特币当前价格"""
    try:
        client = UMFutures()
        btc_price = float(client.mark_price("BTCUSDT")['markPrice'])
        return btc_price, None
    except Exception as e:
        error_msg = f"获取比特币价格失败: {str(e)}"
        logging.error(error_msg)
        return None, error_msg

@cooldown
async def btc_predict_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /btc_predict 命令"""
    user_id = update.effective_user.id
    
    # 检查用户是否已注册
    if not process_user.user_exists(user_id):
        await update.message.reply_text(
            "请先使用 /me 命令注册您的账户。\n"
            "Please register first using the /me command."
        )
        return
    
    # 获取比特币当前价格
    btc_price, error = await get_btc_price()
    if error:
        await update.message.reply_text(f"{error}\n请稍后再试。")
        return
    
    # 检查用户是否已有活跃预测
    active_prediction = await get_user_active_prediction(user_id)
    if (active_prediction):
        remaining_time = active_prediction['end_time'] - datetime.now()
        minutes = int(remaining_time.total_seconds() // 60)
        seconds = int(remaining_time.total_seconds() % 60)
        
        # 显示用户当前预测状态
        direction = "上涨" if active_prediction['predict_type'] == 'up' else "下跌"
        await update.message.reply_text(
            f"⚠️ 您已经有一个正在进行的预测！\n\n"
            f"预测方向: {direction}\n"
            f"投入金额: {active_prediction['amount']} 金币\n"
            f"起始价格: ${active_prediction['start_price']:,.2f}\n"
            f"剩余时间: {minutes}分钟 {seconds}秒\n\n"
            f"请等待此次预测结束后再开始新预测。"
        )
        return
    
    # 如果没有带参数，显示介绍信息
    if not context.args:
        # 创建问额度的键盘，添加用户ID以防止他人点击
        keyboard = [
            [
                InlineKeyboardButton("20 金币", callback_data=f"crypto_amount_20_user_{user_id}"),
                InlineKeyboardButton("50 金币", callback_data=f"crypto_amount_50_user_{user_id}"),
                InlineKeyboardButton("100 金币", callback_data=f"crypto_amount_100_user_{user_id}")
            ],
            [InlineKeyboardButton("自定义金额", callback_data=f"crypto_amount_custom_user_{user_id}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            f"🔮 比特币价格预测 🔮\n\n"
            f"当前比特币价格: ${btc_price:,.2f}\n\n"
            f"游戏规则:\n"
            f"1. 预测10分钟后比特币价格是上涨还是下跌\n"
            f"2. 最低投入20金币\n"
            f"3. 预测正确: 返还投入金额 + 80%奖励\n"
            f"4. 预测错误: 损失全部投入金额\n\n"
            f"📊 [点击查看比特币实时价格图表](https://cn.tradingview.com/chart/?symbol=BINANCE%3ABTCUSDT.P)\n\n"
            f"请选择您要投入的金币数量:",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    # 如果带参数，解析投入金额
    try:
        amount = int(context.args[0])
        await handle_amount_selection(update, context, amount)
    except ValueError:
        await update.message.reply_text(
            "请输入有效的投入金额。格式: /btc_predict <金额>\n"
            "或直接使用 /btc_predict 选择金额。"
        )

async def handle_amount_selection(update, context, amount):
    """处理用户选择的金额"""
    user_id = update.effective_user.id
    
    # 检查最低投入
    if amount < 20:
        await update.message.reply_text(
            "最低投入金额为20金币。请重新选择。"
        )
        return
    
    # 检查用户是否有足够的金币
    user_coins = process_user.get_user_coins(user_id)
    if user_coins < amount:
        await update.message.reply_text(
            f"您的金币不足。当前余额: {user_coins} 金币，需要: {amount} 金币。"
        )
        return
    
    # 获取比特币当前价格
    btc_price, error = await get_btc_price()
    if error:
        await update.message.reply_text(f"{error}\n请稍后再试。")
        return
    
    # 显示选择预测方向的按钮，加入用户ID以防止他人点击
    keyboard = [
        [
            InlineKeyboardButton("预测上涨 ↗", callback_data=f"crypto_predict_up_user_{user_id}_{amount}"),
            InlineKeyboardButton("预测下跌 ↘", callback_data=f"crypto_predict_down_user_{user_id}_{amount}")
        ],
        [InlineKeyboardButton("取消", callback_data=f"crypto_cancel_user_{user_id}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"您准备投入 {amount} 金币进行比特币价格预测。\n"
        f"当前价格: ${btc_price:,.2f}\n\n"
        f"📊 [点击查看比特币实时价格图表](https://cn.tradingview.com/chart/?symbol=BINANCE%3ABTCUSDT.P)\n\n"
        f"请选择您的预测方向:",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )

async def crypto_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理所有与加密货币预测相关的回调"""
    query = update.callback_query
    user_id = query.from_user.id
    
    try:
        # 检查是否在按钮冷却期内
        current_time = time.time()
        if user_id in button_click_cooldown:
            last_click_time = button_click_cooldown[user_id]
            if current_time - last_click_time < CLICK_COOLDOWN_SECONDS:
                await query.answer("请不要频繁点击按钮，请稍等几秒钟。", show_alert=True)
                return
        
        # 更新用户最后点击时间
        button_click_cooldown[user_id] = current_time
        
        # 检查是否是其他用户点击了带有user_id的按钮
        if "_user_" in query.data:
            try:
                target_user_id = int(query.data.split("_user_")[1].split("_")[0])
                if user_id != target_user_id:
                    await query.answer("这不是您的预测，您不能操作他人的预测。", show_alert=True)
                    return
            except (IndexError, ValueError) as e:
                logging.error(f"解析用户ID时出错: {e}, 数据: {query.data}")
                await query.answer("按钮数据格式错误", show_alert=True)
                return
        
        # 首先确认回调
        await query.answer()
        
        # 处理取消操作
        if query.data.startswith("crypto_cancel"):
            await query.edit_message_text("已取消预测。")
            return
        
        # 处理金额选择
        if query.data.startswith("crypto_amount_"):
            # 解析数据
            if "_user_" in query.data:
                try:
                    parts = query.data.split("_user_")[0].split("_")
                    amount_str = parts[2] if len(parts) > 2 else None
                except (IndexError, ValueError) as e:
                    logging.error(f"解析金额字符串时出错: {e}, 数据: {query.data}")
                    await query.edit_message_text("解析金额时发生错误，请使用 /btc_predict 重新开始。")
                    return
            else:
                # 兼容旧格式
                parts = query.data.split("_")
                if len(parts) >= 3:
                    amount_str = parts[2]
                else:
                    await query.edit_message_text("回调数据格式错误，请使用 /btc_predict 重新开始。")
                    return
                    
            if amount_str == "custom":
                await query.edit_message_text(
                    "请直接发送命令指定您要投入的金额，例如:\n"
                    "/btc_predict 100\n\n"
                    "（最低投入20金币）"
                )
                return
            else:
                try:
                    amount = int(amount_str)
                    
                    # 获取比特币当前价格
                    btc_price, error = await get_btc_price()
                    if error:
                        await query.edit_message_text(f"{error}\n请稍后再试。")
                        return
                    
                    # 检查用户是否有足够的金币
                    user_coins = process_user.get_user_coins(user_id)
                    if user_coins < amount:
                        await query.edit_message_text(
                            f"您的金币不足。当前余额: {user_coins} 金币，需要: {amount} 金币。\n"
                            f"请使用 /btc_predict 重新选择金额。"
                        )
                        return
                    
                    # 修改按钮回调数据，加入用户ID
                    keyboard = [
                        [
                            InlineKeyboardButton("预测上涨 ↗", callback_data=f"crypto_predict_up_user_{user_id}_{amount}"),
                            InlineKeyboardButton("预测下跌 ↘", callback_data=f"crypto_predict_down_user_{user_id}_{amount}")
                        ],
                        [InlineKeyboardButton("取消", callback_data=f"crypto_cancel_user_{user_id}")]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    
                    await query.edit_message_text(
                        f"您准备投入 {amount} 金币进行比特币价格预测。\n"
                        f"当前价格: ${btc_price:,.2f}\n\n"
                        f"📊 [点击查看比特币实时价格图表](https://cn.tradingview.com/chart/?symbol=BINANCE%3ABTCUSDT.P)\n\n"
                        f"请选择您的预测方向:",
                        reply_markup=reply_markup,
                        parse_mode=ParseMode.MARKDOWN
                    )
                except ValueError as e:
                    logging.error(f"处理金额回调时出错: {e}")
                    await query.edit_message_text("解析金额时发生错误，请使用 /btc_predict 重新开始。")
                    return
        
        # 处理预测方向选择
        elif query.data.startswith("crypto_predict_"):
            # 解析回调数据，从query.data中去除user_id部分
            original_data = query.data
            if "_user_" in original_data:
                try:
                    parts = original_data.split("_user_")
                    base_parts = parts[0].split("_")  # crypto_predict_up 或 crypto_predict_down
                    if len(base_parts) < 3:
                        raise IndexError("预测方向数据不完整")
                    
                    direction = base_parts[2]  # 'up' 或 'down'
                    
                    # 从user_id后面的部分提取amount
                    user_parts = parts[1].split("_")
                    # user_parts[0] 是用户ID，user_parts[1]是金额
                    if len(user_parts) < 2:
                        raise IndexError("金额数据不完整")
                        
                    amount = int(user_parts[1])
                except (IndexError, ValueError) as e:
                    logging.error(f"解析预测数据时出错: {e}, 数据: {original_data}")
                    await query.edit_message_text("解析预测数据时发生错误，请使用 /btc_predict 重新开始。")
                    return
            else:
                # 如果没有user_id部分（旧格式），保持原有解析逻辑
                parts = original_data.split("_")
                if len(parts) < 4:
                    logging.error(f"预测回调数据格式错误: {original_data}")
                    await query.edit_message_text("回调数据格式错误，请使用 /btc_predict 重新开始。")
                    return
                    
                direction = parts[2]  # 'up' 或 'down'
                try:
                    amount = int(parts[3])
                except (ValueError, IndexError) as e:
                    logging.error(f"解析预测金额时出错: {e}, 数据: {original_data}")
                    await query.edit_message_text("解析预测数据时发生错误，请使用 /btc_predict 重新开始。")
                    return
            
            # 使用用户特定的锁，防止同一用户多次操作冲突
            user_lock = await get_user_lock(user_id)
            # 增加锁的超时控制，防止长时间阻塞
            try:
                # 修改timeout的使用方式，使用asyncio.wait_for替代
                async with user_lock:
                    # 设置任务超时
                    async def locked_operation():
                        # 检查用户是否已有活跃预测 - 这里再次检查是为了防止快速点击导致的并发问题
                        active_prediction = await get_user_active_prediction(user_id)
                        if (active_prediction):
                            await query.answer("您已经有一个正在进行的预测。请等待它结束后再开始新预测。", show_alert=True)
                            return False
                        
                        # 获取当前比特币价格
                        btc_price, error = await get_btc_price()
                        if error:
                            await query.edit_message_text(f"{error}\n请稍后再试。")
                            return False
                        
                        # 创建预测
                        success, error_msg = await create_prediction(user_id, direction, amount, btc_price)
                        if not success:
                            await query.edit_message_text(f"创建预测失败: {error_msg}")
                            return False
                        
                        # 创建任务来检查结果
                        task = asyncio.create_task(
                            schedule_prediction_check(context, query.message.chat_id, user_id)
                        )
                        active_predict_tasks[user_id] = task
                        
                        # 更新消息
                        direction_text = "上涨 ↗" if direction == "up" else "下跌 ↘"
                        await query.edit_message_text(
                            f"🎯 预测已创建!\n\n"
                            f"预测方向: {direction_text}\n"
                            f"投入金额: {amount} 金币\n"
                            f"起始价格: ${btc_price:,.2f}\n"
                            f"结束时间: {(datetime.now() + timedelta(minutes=10)).strftime('%H:%M:%S')}\n\n"
                            f"📊 [点击查看比特币实时价格图表](https://cn.tradingview.com/chart/?symbol=BINANCE%3ABTCUSDT.P)\n\n"
                            f"10分钟后系统将自动检查结果并发送通知。",
                            parse_mode=ParseMode.MARKDOWN
                        )
                        return True
                
                try:
                    # 使用wait_for限制操作时间为5秒
                    success = await asyncio.wait_for(locked_operation(), timeout=5.0)
                    if not success:
                        return  # 如果操作失败，locked_operation内部已经处理了错误消息
                except asyncio.TimeoutError:
                    logging.warning(f"用户 {user_id} 的预测操作超时")
                    await query.edit_message_text("操作超时，请使用 /btc_predict 重新开始。")
                    return
            except Exception as e:
                logging.error(f"锁操作时出错: {e}")
                await query.edit_message_text(f"处理预测时出错，请使用 /btc_predict 重新开始。")
                return
        else:
            logging.warning(f"未知回调数据: {query.data}")
            await query.answer("未知操作，请使用 /btc_predict 重新开始。", show_alert=True)
            
    except Exception as e:
        logging.error(f"处理预测回调时发生未处理异常: {str(e)}")
        try:
            await query.edit_message_text("处理您的请求时发生错误，请使用 /btc_predict 重新开始。")
        except Exception:
            # 如果编辑消息失败，可能是因为消息已经被修改或删除
            await query.answer("处理请求时发生错误，请使用 /btc_predict 重新开始。", show_alert=True)

async def get_user_active_prediction(user_id):
    """获取用户当前活跃的预测"""
    connection = mysql_connection.create_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT predict_type, amount, start_price, start_time, end_time FROM user_btc_predictions "
            "WHERE user_id = %s AND is_completed = FALSE AND end_time > %s",
            (user_id, datetime.now())
        )
        result = cursor.fetchone()
        
        if not result:
            return None
        
        return {
            'predict_type': result[0],
            'amount': result[1],
            'start_price': float(result[2]),
            'start_time': result[3],
            'end_time': result[4]
        }
    except Exception as e:
        logging.error(f"获取用户活跃预测失败: {str(e)}")
        return None
    finally:
        cursor.close()
        connection.close()

async def create_prediction(user_id, predict_type, amount, start_price):
    """创建新的预测记录"""
    connection = mysql_connection.create_connection()
    cursor = connection.cursor()
    try:
        # 检查用户是否已有活跃预测，这是第二重保护
        cursor.execute(
            "SELECT user_id, predict_type, amount, start_price, end_time FROM user_btc_predictions WHERE user_id = %s AND is_completed = FALSE",
            (user_id,)
        )
        existing_prediction = cursor.fetchone()
        
        if existing_prediction:
            # 因为表中没有独立的id字段，所以我们直接用user_id作为标识符
            prediction_user_id, existing_type, existing_amount, existing_start_price, end_time = existing_prediction
            
            # 检查预测是否已过期但未结算
            if end_time < datetime.now():
                logging.warning(f"用户 {user_id} 有过期未结算的预测, 正在进行结算处理")
                
                # 直接返还本金并标记为已完成
                cursor.execute(
                    "UPDATE user SET coins = coins + %s WHERE id = %s",
                    (existing_amount, user_id)
                )
                cursor.execute(
                    "UPDATE user_btc_predictions SET is_completed = TRUE WHERE user_id = %s",
                    (user_id,)
                )
                connection.commit()
                logging.info(f"检测到过期未结算的预测，已返还用户 {user_id} 的本金 {existing_amount} 金币")
            else:
                # 未过期，仍有活跃预测
                return False, "您已经有一个正在进行的预测"
        
        # 创建新的预测
        start_time = datetime.now()
        end_time = start_time + timedelta(minutes=10)
        
        # 检查用户是否有足够的金币
        cursor.execute("SELECT coins FROM user WHERE id = %s", (user_id,))
        result = cursor.fetchone()
        if not result or result[0] < amount:
            return False, "金币不足"
        
        # 删除用户的任何未完成预测（以防还有残留）
        cursor.execute("DELETE FROM user_btc_predictions WHERE user_id = %s", (user_id,))
        
        # 插入新预测记录
        cursor.execute(
            "INSERT INTO user_btc_predictions (user_id, predict_type, amount, start_price, start_time, end_time) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (user_id, predict_type, amount, start_price, start_time, end_time)
        )
        
        # 从用户账户扣除金币
        cursor.execute(
            "UPDATE user SET coins = coins - %s WHERE id = %s",
            (amount, user_id)
        )
        
        connection.commit()
        return True, None
    except Exception as e:
        connection.rollback()
        error_msg = f"创建预测时出错: {str(e)}"
        logging.error(error_msg)
        return False, error_msg
    finally:
        cursor.close()
        connection.close()

async def check_prediction_result(user_id):
    """检查预测结果并更新用户金币"""
    connection = mysql_connection.create_connection()
    cursor = connection.cursor()
    try:
        # 获取用户的预测
        cursor.execute(
            "SELECT predict_type, amount, start_price FROM user_btc_predictions "
            "WHERE user_id = %s AND is_completed = FALSE",
            (user_id,)
        )
        result = cursor.fetchone()
        if not result:
            return None
        
        predict_type = result[0]
        amount = result[1]
        start_price = float(result[2])
        
        # 获取当前比特币价格
        btc_price, error = await get_btc_price()
        if error:
            # 如果获取价格失败，不处理结果，让用户等待后续检查
            return None
        
        # 判断预测是否正确
        price_change = btc_price - start_price
        is_up = price_change > 0
        is_correct = (predict_type == 'up' and is_up) or (predict_type == 'down' and not is_up)
        
        # 标记预测为已完成
        cursor.execute(
            "UPDATE user_btc_predictions SET is_completed = TRUE WHERE user_id = %s AND is_completed = FALSE",
            (user_id,)
        )
        
        # 如果预测正确，返还本金和奖励
        reward = 0
        if is_correct:
            reward = int(amount * 1.8)  # 本金 + 80% 奖励
            cursor.execute(
                "UPDATE user SET coins = coins + %s WHERE id = %s",
                (reward, user_id)
            )
        
        connection.commit()
        
        return {
            'predict_type': predict_type,
            'amount': amount,
            'start_price': start_price,
            'end_price': btc_price,
            'is_correct': is_correct,
            'reward': reward
        }
    except Exception as e:
        connection.rollback()
        logging.error(f"检查预测结果失败: {str(e)}")
        return None
    finally:
        cursor.close()
        connection.close()

async def schedule_prediction_check(context, chat_id, user_id):
    """调度预测结果检查"""
    try:
        # 等待10分钟
        await asyncio.sleep(600)
        
        # 检查预测结果
        result = await check_prediction_result(user_id)
        if not result:
            # 如果获取结果失败，发送通知
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"⚠️ 无法检查您的比特币价格预测结果，请联系管理员。"
            )
            return
        
        # 获取用户名称，用于@通知
        username = await get_username_by_user_id(user_id, context)
        mention_text = f"@{username}" if username else f"用户 {user_id}"
        
        # 发送结果通知
        direction = "上涨 ↗" if result['predict_type'] == 'up' else "下跌 ↘"
        actual_direction = "上涨 ↗" if result['end_price'] > result['start_price'] else "下跌 ↘"
        change_pct = abs((result['end_price'] - result['start_price']) / result['start_price'] * 100)
        
        if result['is_correct']:
            message = (
                f"🎉 {mention_text}，您的比特币价格预测正确！\n\n"
                f"预测方向: {direction}\n"
                f"实际变化: {actual_direction} ({change_pct:.2f}%)\n"
                f"起始价格: ${result['start_price']:,.2f}\n"
                f"结束价格: ${result['end_price']:,.2f}\n\n"
                f"您获得了 {result['reward']} 金币 (本金 + 80% 奖励)！\n\n"
                f"📊 [点击查看比特币实时价格图表](https://cn.tradingview.com/chart/?symbol=BINANCE%3ABTCUSDT.P)"
            )
        else:
            message = (
                f"😞 {mention_text}，您的比特币价格预测错误。\n\n"
                f"预测方向: {direction}\n"
                f"实际变化: {actual_direction} ({change_pct:.2f}%)\n"
                f"起始价格: ${result['start_price']:,.2f}\n"
                f"结束价格: ${result['end_price']:,.2f}\n\n"
                f"您损失了投入的 {result['amount']} 金币。再接再厉！\n\n"
                f"📊 [点击查看比特币实时价格图表](https://cn.tradingview.com/chart/?symbol=BINANCE%3ABTCUSDT.P)"
            )
        
        await context.bot.send_message(
            chat_id=chat_id,
            text=message,
            parse_mode=ParseMode.MARKDOWN
        )
    except asyncio.CancelledError:
        # 处理任务被取消的情况
        logging.info(f"用户 {user_id} 的预测检查任务被取消")
    except Exception as e:
        logging.error(f"调度预测结果检查失败: {str(e)}")
        # 尝试发送错误通知
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text="在处理您的比特币价格预测时发生错误，请联系管理员。"
            )
        except:
            pass
    finally:
        # 清除任务记录
        active_predict_tasks.pop(user_id, None)

async def get_username_by_user_id(user_id, context):
    """获取用户名，用于@通知"""
    try:
        # 尝试获取用户信息
        user = await context.bot.get_chat_member(chat_id=user_id, user_id=user_id)
        if user and user.user and user.user.username:
            return user.user.username
    except Exception as e:
        logging.error(f"从Telegram获取用户名失败: {str(e)}")
    
    # 如果从Telegram获取失败，尝试从数据库获取
    try:
        connection = mysql_connection.create_connection()
        cursor = connection.cursor()
        cursor.execute("SELECT name FROM user WHERE id = %s", (user_id,))
        result = cursor.fetchone()
        cursor.close()
        connection.close()
        
        if result and result[0]:
            return result[0]
    except Exception as e:
        logging.error(f"从数据库获取用户名失败: {str(e)}")
    
    return None

def setup_crypto_predict_handlers(application):
    """为比特币预测功能设置处理器"""
    application.add_handler(CommandHandler("btc_predict", btc_predict_command))
    application.add_handler(CallbackQueryHandler(crypto_callback, pattern=r"^crypto_"))