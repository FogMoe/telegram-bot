import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import RLock
import re
import uuid  # 添加uuid模块导入
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes
from core import balance, config, mysql_connection, process_user
from core.command_cooldown import cooldown
from core.command_privacy import private_chat_only
from core.redaction import log_exception, mask_secret, user_error_notice

logger = logging.getLogger(__name__)

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
    {"price": "4.99", "coins": 200},
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


async def _get_recharge_block_until(user_id: int) -> datetime | None:
    row = await mysql_connection.fetch_one(
        "SELECT recharge_blocked_until FROM user WHERE id = %s",
        (user_id,),
    )
    if not row:
        return None
    blocked_until = row[0]
    return blocked_until if blocked_until else None


def _format_recharge_block_message(blocked_until: datetime) -> str:
    deadline = blocked_until.strftime("%Y-%m-%d %H:%M:%S")
    return f"您暂时无法使用 /recharge，请在 {deadline} 后再试。"

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

            # 卡密行已被 FOR UPDATE 锁住，op_key 以卡密行 id 为身份：同一张卡密最多入账一次。
            # 先入账再标记已使用：用户不存在时在这里得到明确的 UserNotFound，而不是外键错误。
            await balance.credit(
                connection,
                user_id,
                amount,
                op_key=balance.make_op_key("redeem", code_id),
                reason="redeem_code",
                kind=balance.CoinKind.PAID,
            )

            current_time = datetime.now()
            await connection.exec_driver_sql(
                "UPDATE redemption_codes SET is_used = TRUE, used_by = %s, used_at = %s WHERE id = %s",
                (user_id, current_time, code_id),
            )

        return True, amount
    except balance.UserNotFound:
        return False, "请先使用 /me 命令注册个人信息后再使用充值功能"
    except Exception as e:
        error_ref = log_exception(logger, "充值卡密处理错误", e, extra_secrets=(code,))
        return False, f"充值处理过程中出现错误，请联系管理员\n{user_error_notice(error_ref)}"
    finally:
        # 无论成功与否，都释放锁
        with code_lock_mutex:
            if code in code_locks:
                del code_locks[code]


@private_chat_only("charge")
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
    masked_code = mask_secret(redemption_code)
    logging.info(f"用户 {user_name}(ID:{user_id}) 尝试使用卡密: {masked_code}")
    
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
            f"🎟️ 卡密: {masked_code}\n"
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

    blocked_until = await _get_recharge_block_until(user_id)
    if blocked_until and blocked_until > datetime.now():
        await update.message.reply_text(_format_recharge_block_message(blocked_until))
        return

    keyboard = _build_topup_keyboard()
    if not keyboard.inline_keyboard:
        await update.message.reply_text("当前没有可用的充值套餐，请稍后再试。")
        return

    await update.message.reply_text(
        "【充值须知】\n"
        "目前仅支持用户主动私聊管理员充值。请务必核对管理员账号，谨防假冒！官方绝不会主动私信索要财物，请谨慎甄别，拒绝第三方渠道。\n\n"
        "请选择充值套餐，系统会将请求转发给管理员 @ScarletKc ：",
        reply_markup=keyboard,
    )


TOPUP_STATUS_PENDING = "pending"
# 管理员按钮动作 -> 请求的目标状态
TOPUP_ACTION_STATUS = {
    "approve": "approved",
    "reject": "rejected",
    "block": "blocked",
}
TOPUP_STATUS_LABELS = {
    "pending": "待处理",
    "approved": "已发放",
    "rejected": "已拒绝",
    "blocked": "已拒绝并禁用",
}
_TOPUP_ADMIN_CALLBACK = re.compile(r"^topup_admin_(approve|reject|block)_(\d{1,18})$")
# 旧版回调把用户、金币数、价格写进按钮：topup_admin_<action>_<uid>_<coins>_<cents>
_LEGACY_TOPUP_ADMIN_CALLBACK = re.compile(r"^topup_admin_[a-z]+_-?\d+_-?\d+_-?\d+$")


@dataclass(frozen=True)
class TopupRequest:
    id: int
    user_id: int
    coins: int
    price_cents: int
    status: str


@dataclass(frozen=True)
class TopupDecision:
    """`decide_topup_request` 的结果。

    outcome:
      applied          这次调用完成了 pending -> 目标状态的转换（approve 同时已入账）
      already_decided  请求已被处理过，`request.status` 是当前状态，没有任何改动
      not_found        没有这个请求
      user_missing     approve 时用户已不存在，整个事务回滚，请求仍是 pending
    """

    outcome: str
    request: TopupRequest | None = None
    credit: balance.BalanceResult | None = None
    blocked_until: datetime | None = None


def topup_op_key(request_id: int) -> str:
    return balance.make_op_key("topup", request_id)


def topup_admin_callback_data(action: str, request_id: int) -> str:
    return f"topup_admin_{action}_{request_id}"


def parse_topup_admin_callback(data: str) -> tuple[str, int] | None:
    """新格式 `topup_admin_<action>_<request_id>` -> (action, request_id)，其他一律 None。"""
    match = _TOPUP_ADMIN_CALLBACK.fullmatch(data or "")
    if not match:
        return None
    return match.group(1), int(match.group(2))


def is_legacy_topup_admin_callback(data: str) -> bool:
    return bool(_LEGACY_TOPUP_ADMIN_CALLBACK.fullmatch(data or ""))


def _topup_request_from_row(row) -> TopupRequest:
    return TopupRequest(
        id=int(row[0]),
        user_id=int(row[1]),
        coins=int(row[2]),
        price_cents=int(row[3]),
        status=str(row[4]),
    )


async def create_topup_request(user_id: int, coins: int, price_cents: int) -> int:
    """记录一条 pending 的充值请求并返回它的 id；管理员按钮只携带这个 id。"""
    async with mysql_connection.transaction() as connection:
        result = await connection.exec_driver_sql(
            "INSERT INTO topup_requests (user_id, coins, price_cents) VALUES (%s, %s, %s)",
            (user_id, coins, price_cents),
        )
        return int(result.lastrowid)


async def get_topup_request(request_id: int, *, connection=None) -> TopupRequest | None:
    row = await mysql_connection.fetch_one(
        "SELECT id, user_id, coins, price_cents, status FROM topup_requests WHERE id = %s",
        (request_id,),
        connection=connection,
    )
    return _topup_request_from_row(row) if row else None


async def discard_pending_topup_request(request_id: int) -> None:
    """请求没能送达管理员时撤销它；已经被处理过的请求不受影响。"""
    await mysql_connection.execute(
        "DELETE FROM topup_requests WHERE id = %s AND status = 'pending'",
        (request_id,),
    )


async def decide_topup_request(
    request_id: int,
    action: str,
    decided_by: int,
    *,
    now: datetime | None = None,
) -> TopupDecision:
    """管理员对充值请求做出决定：pending 只能转换一次。

    先用 `UPDATE ... WHERE id = %s AND status = 'pending'` 占住转换，影响行数为 1 才继续
    （approve 以 `topup:<id>` 入账付费金币，block 写入禁用截止时间），整个过程一个事务：
    入账失败时状态保持 pending。两个并发的 approve 只有一个能占住转换。
    """
    new_status = TOPUP_ACTION_STATUS[action]
    now = now or datetime.now()
    request = await get_topup_request(request_id)
    if request is None:
        return TopupDecision("not_found")

    try:
        async with mysql_connection.transaction() as connection:
            changed = await mysql_connection.execute(
                "UPDATE topup_requests SET status = %s, decided_at = %s, decided_by = %s "
                "WHERE id = %s AND status = 'pending'",
                (new_status, now, decided_by, request_id),
                connection=connection,
            )
            if changed != 1:
                # 锁定读取到的是最新状态；事务里更早的普通读可能停留在旧快照。
                row = await mysql_connection.fetch_one(
                    "SELECT status FROM topup_requests WHERE id = %s FOR UPDATE",
                    (request_id,),
                    connection=connection,
                )
                current = str(row[0]) if row else "unknown"
                return TopupDecision(
                    "already_decided",
                    TopupRequest(
                        request.id, request.user_id, request.coins, request.price_cents, current
                    ),
                )

            credit = None
            blocked_until = None
            if action == "approve":
                credit = await balance.credit(
                    connection,
                    request.user_id,
                    request.coins,
                    op_key=topup_op_key(request.id),
                    reason="topup",
                    kind=balance.CoinKind.PAID,
                )
            elif action == "block":
                blocked_until = now + timedelta(days=1)
                await mysql_connection.execute(
                    "UPDATE user SET recharge_blocked_until = %s WHERE id = %s",
                    (blocked_until, request.user_id),
                    connection=connection,
                )
    except balance.UserNotFound:
        return TopupDecision("user_missing", request)

    return TopupDecision(
        "applied",
        TopupRequest(request.id, request.user_id, request.coins, request.price_cents, new_status),
        credit,
        blocked_until,
    )


async def topup_request_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user_name = query.from_user.username or str(user_id)

    blocked_until = await _get_recharge_block_until(user_id)
    if blocked_until and blocked_until > datetime.now():
        await query.edit_message_text(_format_recharge_block_message(blocked_until))
        return

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
    if coins <= 0 or price_cents < 0:
        await query.edit_message_text("充值请求数据无效，请重新发起。")
        return

    price_label = _format_price(price_cents)
    request_id = await create_topup_request(user_id, coins, price_cents)
    admin_text = (
        "收到充值请求：\n"
        f"请求编号: #{request_id}\n"
        f"用户: @{user_name} (ID: {user_id})\n"
        f"套餐: {price_label} -> {coins}金币\n"
        "请核对付款后点击下方按钮处理。"
    )
    admin_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("确认发放", callback_data=topup_admin_callback_data("approve", request_id))],
        [InlineKeyboardButton("拒绝", callback_data=topup_admin_callback_data("reject", request_id))],
        [InlineKeyboardButton("禁用1天", callback_data=topup_admin_callback_data("block", request_id))],
    ])

    try:
        await context.bot.send_message(
            chat_id=ADMIN_USER_ID,
            text=admin_text,
            reply_markup=admin_keyboard,
        )
    except Exception as send_error:
        logging.error("发送充值请求给管理员失败: %s", send_error)
        try:
            await discard_pending_topup_request(request_id)
        except Exception as discard_error:
            logging.error("撤销未送达的充值请求失败: %s", discard_error)
        await query.edit_message_text("联系管理员失败，请稍后再试。")
        return

    await query.edit_message_text(
        f"已通知管理员 @ScarletKc 处理您的充值请求（{price_label} -> {coins}金币）。"
    )


async def topup_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.from_user.id != ADMIN_USER_ID:
        await query.answer("您没有权限处理该请求。", show_alert=True)
        return
    await query.answer()

    parsed = parse_topup_admin_callback(query.data)
    if parsed is None:
        if is_legacy_topup_admin_callback(query.data):
            # 旧按钮没有持久的请求身份，无法保证只处理一次：一律拒绝，绝不入账。
            await query.edit_message_text(
                "这是旧版本发出的充值按钮，已失效，不会发放金币。\n"
                "请让用户重新发起 /recharge。"
            )
        else:
            await query.edit_message_text("请求数据无效。")
        return
    action, request_id = parsed

    request = await get_topup_request(request_id)
    if request is None:
        await query.edit_message_text(f"充值请求不存在（编号: #{request_id}）。")
        return
    target_user_id = request.user_id
    coins = request.coins
    price_label = _format_price(request.price_cents)

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

    decision = await decide_topup_request(request_id, action, query.from_user.id)
    if decision.outcome == "user_missing":
        await query.edit_message_text(
            f"用户不存在，无法处理充值请求（ID: {target_user_id}）。"
        )
        return
    if decision.outcome == "not_found":
        await query.edit_message_text(f"充值请求不存在（编号: #{request_id}）。")
        return
    if decision.outcome == "already_decided":
        status = decision.request.status if decision.request else "unknown"
        await query.edit_message_text(
            f"该充值请求已处理，不会重复发放（编号: #{request_id}，"
            f"当前状态: {TOPUP_STATUS_LABELS.get(status, status)}）。\n"
            f"用户: {user_name} (ID: {target_user_id})"
        )
        return

    if action == "approve":
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
                text=f"充值请求未通过（{price_label}）。如有疑问请联系管理员 @ScarletKc 。",
            )
        except Exception as notify_error:
            logging.error("通知用户充值失败: %s", notify_error)
        return

    # action == "block"
    blocked_until = decision.blocked_until
    await query.edit_message_text(
        f"已禁止用户 1 天内使用 /recharge。\n"
        f"用户: {user_name} (ID: {target_user_id})\n"
        f"截止时间: {blocked_until.strftime('%Y-%m-%d %H:%M:%S')}"
    )
    try:
        await context.bot.send_message(
            chat_id=target_user_id,
            text=_format_recharge_block_message(blocked_until),
        )
    except Exception as notify_error:
        logging.error("通知用户禁用失败: %s", notify_error)


@private_chat_only("create_code")
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
        error_ref = log_exception(logger, "生成卡密出错", e)
        await update.message.reply_text(
            f"❌ 生成卡密时出错，请查看日志。\n{user_error_notice(error_ref)}"
        )


def setup_charge_handlers(application):
    """设置充值系统的处理器"""
    application.add_handler(CommandHandler("charge", charge_command))
    application.add_handler(CommandHandler("create_code", admin_create_code))
    application.add_handler(CommandHandler("recharge", recharge_command))
    application.add_handler(CallbackQueryHandler(topup_request_callback, pattern=r"^topup_req_"))
    application.add_handler(CallbackQueryHandler(topup_admin_callback, pattern=r"^topup_admin_"))
