"""充值相关命令的 Telegram 适配层：/charge、/recharge、管理员处理充值请求、/create_code。

兑换、充值请求的决定与卡密生成的规则和事务在 `operations/charge.py`；这里只做输入映射、
按钮与文案、通知管理员和用户。
"""

import logging
import re
from datetime import datetime
from decimal import ROUND_DOWN, Decimal, InvalidOperation

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from fogmoe_telegram_bot.core import config, process_user, user_records
from fogmoe_telegram_bot.core.command_cooldown import cooldown
from fogmoe_telegram_bot.core.command_privacy import private_chat_only
from fogmoe_telegram_bot.core.redaction import log_exception, mask_secret, user_error_notice

from .operations import charge as charge_operations
from .operations.charge import (
    DecisionOutcome,
    RedeemResult,
    RedeemStatus,
    TopupAction,
    is_valid_uuid,
)

logger = logging.getLogger(__name__)

TOPUP_PACKAGES = [
    {"price": "1.99", "coins": 50},
    {"price": "2.99", "coins": 100},
    {"price": "4.99", "coins": 200},
]
TOPUP_CURRENCY = "$"
TOPUP_PRICE_QUANT = Decimal("0.01")


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


def _format_recharge_block_message(blocked_until: datetime) -> str:
    deadline = blocked_until.strftime("%Y-%m-%d %H:%M:%S")
    return f"您暂时无法使用 /recharge，请在 {deadline} 后再试。"


def _redeem_failure_message(result: RedeemResult) -> str:
    """兑换没有成功时给用户看的原因。"""
    if result.status is RedeemStatus.INVALID_FORMAT:
        return "卡密格式无效，请确保输入了正确的充值卡密"
    if result.status is RedeemStatus.BUSY:
        return "此卡密正在被其他用户处理，请稍后再试"
    if result.status is RedeemStatus.NOT_FOUND:
        return "无效的充值卡密，此卡密不存在或已被删除"
    if result.status is RedeemStatus.ALREADY_USED:
        used_time = (
            result.used_at.strftime("%Y-%m-%d %H:%M:%S") if result.used_at else "未知时间"
        )
        if result.used_by_self:
            return f"此卡密已被您在 {used_time} 使用过"
        return f"此卡密已被其他用户在 {used_time} 使用"
    if result.status is RedeemStatus.NOT_REGISTERED:
        return "请先使用 /me 命令注册个人信息后再使用充值功能"
    return f"充值处理过程中出现错误，请联系管理员\n{user_error_notice(result.error_ref)}"


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
    redeemed = await charge_operations.redeem_code(user_id, redemption_code)

    if redeemed.status is RedeemStatus.REDEEMED:
        amount = redeemed.amount
        # 充值成功，获取用户当前金币
        current_coins = await process_user.async_get_user_coins(user_id)
        previous_coins = current_coins - amount

        # 记录成功充值日志
        logging.info(f"用户 {user_name}(ID:{user_id}) 成功充值 {amount} 金币，当前余额: {current_coins}")

        # 充值成功消息
        await processing_msg.edit_text(
            f"✅ 充值成功！\n\n"
            f"🎟️ 卡密: {masked_code}\n"
            f"💰 充值金额: +{amount} 金币\n"
            f"💳 充值前余额: {previous_coins} 金币\n"
            f"💎 当前余额: {current_coins} 金币\n\n"
            f"感谢您的支持！\n\n"
            f"Charge successful!\n"
            f"Added: {amount} coins\n"
            f"Current balance: {current_coins} coins\n"
            f"Thank you for your support!"
        )
    else:
        reason = _redeem_failure_message(redeemed)
        # 记录充值失败日志
        logging.warning(f"用户 {user_name}(ID:{user_id}) 充值失败: {reason}")

        # 充值失败，显示错误消息
        await processing_msg.edit_text(
            f"❌ 充值失败\n"
            f"原因: {reason}\n\n"
            f"如需帮助，请联系机器人管理员 @ScarletKc\n\n"
            f"Charge failed\n"
            f"Reason: {reason}\n"
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

    blocked_until = await charge_operations.get_recharge_blocked_until(user_id)
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


TOPUP_STATUS_LABELS = {
    "pending": "待处理",
    "approved": "已发放",
    "rejected": "已拒绝",
    "blocked": "已拒绝并禁用",
}
_TOPUP_ADMIN_CALLBACK = re.compile(r"^topup_admin_(approve|reject|block)_(\d{1,18})$")
# 旧版回调把用户、金币数、价格写进按钮：topup_admin_<action>_<uid>_<coins>_<cents>
_LEGACY_TOPUP_ADMIN_CALLBACK = re.compile(r"^topup_admin_[a-z]+_-?\d+_-?\d+_-?\d+$")


def topup_admin_callback_data(action: str, request_id: int) -> str:
    return f"topup_admin_{action}_{request_id}"


def parse_topup_admin_callback(data: str) -> tuple[TopupAction, int] | None:
    """新格式 `topup_admin_<action>_<request_id>` -> (action, request_id)，其他一律 None。"""
    match = _TOPUP_ADMIN_CALLBACK.fullmatch(data or "")
    if not match:
        return None
    return TopupAction(match.group(1)), int(match.group(2))


def is_legacy_topup_admin_callback(data: str) -> bool:
    return bool(_LEGACY_TOPUP_ADMIN_CALLBACK.fullmatch(data or ""))


async def topup_request_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user_name = query.from_user.username or str(user_id)

    blocked_until = await charge_operations.get_recharge_blocked_until(user_id)
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
    request_id = await charge_operations.create_topup_request(user_id, coins, price_cents)
    admin_text = (
        "收到充值请求：\n"
        f"请求编号: #{request_id}\n"
        f"用户: @{user_name} (ID: {user_id})\n"
        f"套餐: {price_label} -> {coins}金币\n"
        "请核对付款后点击下方按钮处理。"
    )
    admin_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("确认发放", callback_data=topup_admin_callback_data(TopupAction.APPROVE, request_id))],
        [InlineKeyboardButton("拒绝", callback_data=topup_admin_callback_data(TopupAction.REJECT, request_id))],
        [InlineKeyboardButton("禁用1天", callback_data=topup_admin_callback_data(TopupAction.BLOCK, request_id))],
    ])

    try:
        await context.bot.send_message(
            chat_id=config.ADMIN_USER_ID,
            text=admin_text,
            reply_markup=admin_keyboard,
        )
    except Exception as send_error:
        logging.error("发送充值请求给管理员失败: %s", send_error)
        try:
            await charge_operations.discard_pending_topup_request(request_id)
        except Exception as discard_error:
            logging.error("撤销未送达的充值请求失败: %s", discard_error)
        await query.edit_message_text("联系管理员失败，请稍后再试。")
        return

    await query.edit_message_text(
        f"已通知管理员 @ScarletKc 处理您的充值请求（{price_label} -> {coins}金币）。"
    )


async def topup_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.from_user.id != config.ADMIN_USER_ID:
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

    request = await charge_operations.get_topup_request(request_id)
    if request is None:
        await query.edit_message_text(f"充值请求不存在（编号: #{request_id}）。")
        return
    target_user_id = request.user_id
    coins = request.coins
    price_label = _format_price(request.price_cents)

    user_name = await user_records.get_name(target_user_id)
    if user_name is None:
        await query.edit_message_text(
            f"用户不存在，无法处理充值请求（ID: {target_user_id}）。"
        )
        return

    decision = await charge_operations.decide_topup_request(
        request_id, action, query.from_user.id
    )
    if decision.outcome is DecisionOutcome.USER_MISSING:
        await query.edit_message_text(
            f"用户不存在，无法处理充值请求（ID: {target_user_id}）。"
        )
        return
    if decision.outcome is DecisionOutcome.NOT_FOUND:
        await query.edit_message_text(f"充值请求不存在（编号: #{request_id}）。")
        return
    if decision.outcome is DecisionOutcome.ALREADY_DECIDED:
        status = decision.request.status if decision.request else "unknown"
        await query.edit_message_text(
            f"该充值请求已处理，不会重复发放（编号: #{request_id}，"
            f"当前状态: {TOPUP_STATUS_LABELS.get(status, status)}）。\n"
            f"用户: {user_name} (ID: {target_user_id})"
        )
        return

    if action is TopupAction.APPROVE:
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

    if action is TopupAction.REJECT:
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
    if user_id != config.ADMIN_USER_ID:
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
        generated = await charge_operations.generate_codes(count, amount)
        codes = generated.codes
        duplicate_count = generated.duplicate_count

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
