import logging
import asyncio
from datetime import datetime
from threading import RLock
import re
import uuid  # 添加uuid模块导入
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes
from core import config, mysql_connection, process_user
from core.command_cooldown import cooldown

# 创建一个锁字典，用于防止同一卡密被并发使用
code_locks = {}
code_lock_mutex = RLock()  # 控制对code_locks字典的访问

# UUID格式的正则表达式
UUID_PATTERN = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.IGNORECASE)

# 管理员ID，用于权限验证
ADMIN_USER_ID = config.ADMIN_USER_ID  # 管理员的Telegram UserID
TOPUP_PACKAGES = [
    {"price": "1.99", "coins": 50},
    {"price": "2.99", "coins": 100},
]
TOPUP_CURRENCY = "$"
TOPUP_PRICE_QUANT = Decimal("0.01")

def is_valid_uuid(code):
    """验证字符串是否为有效的UUID格式"""
    return bool(UUID_PATTERN.match(code))


def _price_to_cents(price: str) -> int:
    try:
        value = Decimal(price).quantize(TOPUP_PRICE_QUANT, rounding=ROUND_DOWN)
    except (InvalidOperation, TypeError):
        return 0
    return int(value * 100)


def _format_price(cents: int) -> str:
    price = (Decimal(cents) / Decimal(100)).quantize(TOPUP_PRICE_QUANT, rounding=ROUND_DOWN)
    return f"{TOPUP_CURRENCY}{price}"


def _build_topup_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for pkg in TOPUP_PACKAGES:
        price_cents = _price_to_cents(pkg["price"])
        if price_cents <= 0:
            continue
        label = f"{TOPUP_CURRENCY}{pkg['price']} - {pkg['coins']}金币"
        rows.append([InlineKeyboardButton(label, callback_data=f"topup_req_{price_cents}_{pkg['coins']}")])
    return InlineKeyboardMarkup(rows)

async def verify_and_use_code(user_id: int, code: str) -> tuple:
    """
    验证卡密并使用，确保原子操作
    
    返回: (成功与否, 金币数量或错误消息)
    """
    # 验证UUID格式
    if not is_valid_uuid(code):
        return False, "卡密格式无效，请确保输入了正确的充值卡密"
    
    # 先获取锁，防止同一卡密被并发请求使用
    with code_lock_mutex:
        if code in code_locks:
            return False, "此卡密正在被其他用户处理，请稍后再试"
        code_locks[code] = True

    try:
        async with mysql_connection.transaction() as connection:
            result = await mysql_connection.fetch_one(
                "SELECT id, code, amount, is_used, used_by, used_at FROM redemption_codes WHERE code = %s FOR UPDATE",
                (code,),
                connection=connection,
            )
            if not result:
                return False, "无效的充值卡密，此卡密不存在或已被删除"

            code_id, _, amount, is_used, used_by, used_at = result

            if is_used:
                used_time = used_at.strftime("%Y-%m-%d %H:%M:%S") if used_at else "未知时间"
                if used_by == user_id:
                    used_msg = f"此卡密已被您在 {used_time} 使用过"
                else:
                    used_msg = f"此卡密已被其他用户在 {used_time} 使用"
                return False, used_msg

            current_time = datetime.now()
            await connection.exec_driver_sql(
                "UPDATE redemption_codes SET is_used = TRUE, used_by = %s, used_at = %s WHERE id = %s",
                (user_id, current_time, code_id),
            )

            await connection.exec_driver_sql(
                "UPDATE user SET coins = coins + %s WHERE id = %s",
                (amount, user_id),
            )

        return True, amount
    except Exception as e:
        logging.error(f"充值卡密处理错误: {str(e)}")
        return False, "充值处理过程中出现错误，请联系管理员"
    finally:
        # 无论成功与否，都释放锁
        with code_lock_mutex:
            if code in code_locks:
                del code_locks[code]


@cooldown
async def charge_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理充值命令: /charge <卡密>"""
    user_id = update.effective_user.id
    user_name = update.effective_user.username or str(user_id)
    
    # 检查用户是否已注册
    if not await process_user.async_user_exists(user_id):
        await update.message.reply_text(
            "❌ 请先使用 /me 命令注册个人信息后再使用充值功能。\n"
            "Please register first using the /me command before charging."
        )
        return
    
    # 检查是否提供了卡密参数
    if not context.args or len(context.args) != 1:
        await update.message.reply_text(
            "⚠️ 请输入正确的充值卡密！\n"
            "使用方法: /charge <卡密码>\n\n"
            "🔹 卡密格式例如: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx\n\n"
            "Please enter a valid redemption code!\n"
            "Usage: /charge <code>"
        )
        return
    
    # 获取卡密
    redemption_code = context.args[0].strip()
    
    # UUID格式预检查，避免明显错误的格式直接提交数据库
    if not is_valid_uuid(redemption_code):
        await update.message.reply_text(
            "❌ 卡密格式不正确！\n"
            "🔹 正确的卡密格式应为: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx\n"
            "例如: 123e4567-e89b-12d3-a456-426614174000\n\n"
            "Invalid code format! The correct format should be:\n"
            "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
        )
        return
    
    # 记录充值尝试
    logging.info(f"用户 {user_name}(ID:{user_id}) 尝试使用卡密: {redemption_code}")
    
    # 发送处理中消息
    processing_msg = await update.message.reply_text(
        "⏳ 正在处理您的充值请求，请稍候...\n"
        "Processing your charge request, please wait..."
    )
    
    # 验证并使用卡密
    success, result = await verify_and_use_code(user_id, redemption_code)
    
    if success:
        # 充值成功，获取用户当前金币
        current_coins = await process_user.async_get_user_coins(user_id)
        previous_coins = current_coins - result
        
        # 记录成功充值日志
        logging.info(f"用户 {user_name}(ID:{user_id}) 成功充值 {result} 金币，当前余额: {current_coins}")
        
        # 充值成功消息
        await processing_msg.edit_text(
            f"✅ 充值成功！\n\n"
            f"🎟️ 卡密: {redemption_code}\n"
            f"💰 充值金额: +{result} 金币\n"
            f"💳 充值前余额: {previous_coins} 金币\n"
            f"💎 当前余额: {current_coins} 金币\n\n"
            f"感谢您的支持！\n\n"
            f"Charge successful!\n"
            f"Added: {result} coins\n"
            f"Current balance: {current_coins} coins\n"
            f"Thank you for your support!"
        )
    else:
        # 记录充值失败日志
        logging.warning(f"用户 {user_name}(ID:{user_id}) 充值失败: {result}")
        
        # 充值失败，显示错误消息
        await processing_msg.edit_text(
            f"❌ 充值失败\n"
            f"原因: {result}\n\n"
            f"如需帮助，请联系机器人管理员 @ScarletKc\n\n"
            f"Charge failed\n"
            f"Reason: {result}\n"
            f"For assistance, please contact the bot admin @ScarletKc"
        )


@cooldown
async def recharge_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """联系管理员充值金币"""
    user_id = update.effective_user.id

    if not await process_user.async_user_exists(user_id):
        await update.message.reply_text(
            "❌ 请先使用 /me 命令注册个人信息后再使用充值功能。\n"
            "Please register first using the /me command before charging."
        )
        return

    keyboard = _build_topup_keyboard()
    if not keyboard.inline_keyboard:
        await update.message.reply_text("当前没有可用的充值套餐，请稍后再试。")
        return

    await update.message.reply_text(
        "请选择充值套餐，系统会将请求转发给管理员：",
        reply_markup=keyboard,
    )


async def topup_request_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user_name = query.from_user.username or str(user_id)

    parts = query.data.split("_")
    if len(parts) != 4:
        await query.edit_message_text("充值请求数据无效，请重新发起。")
        return

    try:
        price_cents = int(parts[2])
        coins = int(parts[3])
    except ValueError:
        await query.edit_message_text("充值请求数据无效，请重新发起。")
        return

    price_label = _format_price(price_cents)
    admin_text = (
        "收到充值请求：\n"
        f"用户: @{user_name} (ID: {user_id})\n"
        f"套餐: {price_label} -> {coins}金币\n"
        "请核对付款后点击下方按钮处理。"
    )
    admin_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("确认发放", callback_data=f"topup_admin_approve_{user_id}_{coins}_{price_cents}")],
        [InlineKeyboardButton("拒绝", callback_data=f"topup_admin_reject_{user_id}_{coins}_{price_cents}")],
    ])

    try:
        await context.bot.send_message(
            chat_id=ADMIN_USER_ID,
            text=admin_text,
            reply_markup=admin_keyboard,
        )
    except Exception as send_error:
        logging.error("发送充值请求给管理员失败: %s", send_error)
        await query.edit_message_text("联系管理员失败，请稍后再试。")
        return

    await query.edit_message_text(
        f"已通知管理员处理您的充值请求（{price_label} -> {coins}金币）。"
    )


async def topup_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.from_user.id != ADMIN_USER_ID:
        await query.answer("您没有权限处理该请求。", show_alert=True)
        return
    await query.answer()

    parts = query.data.split("_")
    if len(parts) != 6:
        await query.edit_message_text("请求数据无效。")
        return

    action = parts[2]
    try:
        target_user_id = int(parts[3])
        coins = int(parts[4])
        price_cents = int(parts[5])
    except ValueError:
        await query.edit_message_text("请求数据无效。")
        return

    price_label = _format_price(price_cents)
    user_row = await mysql_connection.fetch_one(
        "SELECT name FROM user WHERE id = %s",
        (target_user_id,),
    )
    if not user_row:
        await query.edit_message_text(
            f"用户不存在，无法处理充值请求（ID: {target_user_id}）。"
        )
        return
    user_name = user_row[0]

    if action == "approve":
        if coins <= 0:
            await query.edit_message_text("金币数量无效，无法发放。")
            return
        await process_user.async_update_user_coins(target_user_id, coins)
        await query.edit_message_text(
            f"已发放充值：{price_label} -> {coins}金币\n用户: {user_name} (ID: {target_user_id})"
        )
        try:
            await context.bot.send_message(
                chat_id=target_user_id,
                text=f"充值成功！已到账 {coins} 金币（{price_label}）。",
            )
        except Exception as notify_error:
            logging.error("通知用户充值成功失败: %s", notify_error)
        return

    if action == "reject":
        await query.edit_message_text(
            f"已拒绝充值请求：{price_label} -> {coins}金币\n用户: {user_name} (ID: {target_user_id})"
        )
        try:
            await context.bot.send_message(
                chat_id=target_user_id,
                text=f"充值请求未通过（{price_label}）。如有疑问请联系管理员。",
            )
        except Exception as notify_error:
            logging.error("通知用户充值失败: %s", notify_error)
        return

    await query.edit_message_text("未知操作。")


@cooldown
async def admin_create_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """管理员命令：创建充值卡密 /create_code <数量> <金币>"""
    user_id = update.effective_user.id
    
    # 验证管理员权限 - 使用ADMIN_USER_ID常量
    if user_id != ADMIN_USER_ID:
        await update.message.reply_text("❌ 您没有足够的权限执行此操作\n您不是管理员")
        return
    
    # 检查参数格式
    if not context.args or len(context.args) != 2:
        await update.message.reply_text(
            "⚠️ 使用方法: /create_code <生成数量> <每个卡密的金币数>\n"
            "例如: /create_code 5 100"
        )
        return
    
    try:
        count = int(context.args[0])
        amount = int(context.args[1])
        
        if count <= 0 or count > 20:
            await update.message.reply_text("⚠️ 生成数量必须在1-20之间")
            return
        
        if amount <= 0 or amount > 10000:
            await update.message.reply_text("⚠️ 金币数量必须在1-10000之间")
            return
        
    except ValueError:
        await update.message.reply_text("⚠️ 参数必须为整数数字")
        return
    
    try:
        codes = []
        duplicate_count = 0
        max_retries = 3  # 最大重试次数

        async with mysql_connection.transaction() as connection:
            for _ in range(count):
                retry_count = 0
                while retry_count < max_retries:
                    unique_code = str(uuid.uuid4())
                    exists = await mysql_connection.fetch_one(
                        "SELECT id FROM redemption_codes WHERE code = %s",
                        (unique_code,),
                        connection=connection,
                    )
                    if not exists:
                        await connection.exec_driver_sql(
                            "INSERT INTO redemption_codes (code, amount) VALUES (%s, %s)",
                            (unique_code, amount),
                        )
                        codes.append(unique_code)
                        break
                    retry_count += 1

                if retry_count >= max_retries:
                    duplicate_count += 1
                    logging.warning(f"生成唯一卡密失败，重试次数达到上限: {max_retries}")

        if duplicate_count > 0:
            await update.message.reply_text(
                f"⚠️ 注意: 有 {duplicate_count} 个卡密因重复而未能生成。实际生成了 {len(codes)} 个卡密。"
            )

        if not codes:
            await update.message.reply_text("❌ 未能生成任何卡密，请稍后再试")
            return
            
        # 生成卡密列表文本
        codes_text = "\n\n".join([f"{i+1}. `{code}` - {amount}金币" for i, code in enumerate(codes)])
        
        await update.message.reply_text(
            f"✅ 成功生成 {len(codes)} 个充值卡密，每个价值 {amount} 金币：\n\n"
            f"{codes_text}\n\n"
            f"💡 提示：请保存这些卡密，它们只会显示一次！"
        )
        
        # 记录操作日志
        logging.info(f"管理员 {update.effective_user.username or user_id} 生成了 {len(codes)} 个价值 {amount} 金币的卡密")
        
    except Exception as e:
        logging.error(f"生成卡密出错: {str(e)}")
        await update.message.reply_text(f"❌ 生成卡密时出错: {str(e)}")


def setup_charge_handlers(application):
    """设置充值系统的处理器"""
    application.add_handler(CommandHandler("charge", charge_command))
    application.add_handler(CommandHandler("create_code", admin_create_code))
    application.add_handler(CommandHandler("recharge", recharge_command))
    application.add_handler(CallbackQueryHandler(topup_request_callback, pattern=r"^topup_req_"))
    application.add_handler(CallbackQueryHandler(topup_admin_callback, pattern=r"^topup_admin_"))
