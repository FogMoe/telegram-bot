"""商店的展示：菜单、按钮回调数据，以及购买结果给用户看的文案。

只依赖 Telegram 的按钮类型和 `operations.shop` 的结果类型，不碰数据库、不开事务、不发消息；
发送与编辑由适配层（`shop.py`）负责。
"""

from dataclasses import dataclass
from enum import StrEnum

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .operations import shop as shop_purchases
from .operations.shop import PurchaseStatus, UpgradeRefusal

# 按钮回调数据；注册的 pattern 是 `^shop_`。
CALLBACK_HOME = "shop_home"
CALLBACK_CLOSE = "shop_close"
CALLBACK_PERMISSION_MENU = "shop_buy_permission"
CALLBACK_MEMORY_LIMIT = "shop_buy_memory_limit"
CALLBACK_LOTTERY_MENU = "shop_buy_lottery"
CALLBACK_SCRATCH = "shop_scratch"
CALLBACK_HUANLE = "shop_huanle"
UPGRADE_CALLBACKS = {
    "shop_upgrade_1": 1,
    "shop_upgrade_2": 2,
    "shop_upgrade_3": 3,
}

NOT_REGISTERED_MESSAGE = "请先使用 /me 命令获取个人信息。"
INSUFFICIENT_MESSAGE = "硬币不足，无法购买此商品。"

_UPGRADE_REFUSAL_TEXT = {
    UpgradeRefusal.ALREADY_UPGRADED: "您已经拥有权限或已升级。",
    UpgradeRefusal.NEED_LEVEL_1: "您需要先升级到1级权限。",
    UpgradeRefusal.HAS_LEVEL_2: "您已经拥有2级或更高权限。",
    UpgradeRefusal.NEED_LEVEL_2: "您需要先升级到2级权限。",
    UpgradeRefusal.HAS_LEVEL_3: "您已经拥有3级或更高权限。",
}


# ---------------------------------------------------------------------------
# 回调数据 -> 动作
# ---------------------------------------------------------------------------


class Action(StrEnum):
    PERMISSION_MENU = "permission_menu"
    LOTTERY_MENU = "lottery_menu"
    HOME = "home"
    CLOSE = "close"
    BUY_MEMORY_LIMIT = "buy_memory_limit"
    UPGRADE_PERMISSION = "upgrade_permission"
    BUY_SCRATCH = "buy_scratch"
    BUY_HUANLE = "buy_huanle"


@dataclass(frozen=True, slots=True)
class ShopCallback:
    action: Action
    level: int | None = None  # 仅 UPGRADE_PERMISSION：目标等级


_SIMPLE_ACTIONS = {
    CALLBACK_PERMISSION_MENU: Action.PERMISSION_MENU,
    CALLBACK_LOTTERY_MENU: Action.LOTTERY_MENU,
    CALLBACK_MEMORY_LIMIT: Action.BUY_MEMORY_LIMIT,
    CALLBACK_HOME: Action.HOME,
    CALLBACK_CLOSE: Action.CLOSE,
    CALLBACK_SCRATCH: Action.BUY_SCRATCH,
    CALLBACK_HUANLE: Action.BUY_HUANLE,
}


def parse_callback(data: str | None) -> ShopCallback | None:
    """按钮回调数据 -> 动作；不认识的数据返回 None。"""
    if data in UPGRADE_CALLBACKS:
        return ShopCallback(Action.UPGRADE_PERMISSION, UPGRADE_CALLBACKS[data])
    action = _SIMPLE_ACTIONS.get(data or "")
    return None if action is None else ShopCallback(action)


# ---------------------------------------------------------------------------
# 菜单
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Menu:
    text: str
    keyboard: InlineKeyboardMarkup


def home_menu() -> Menu:
    keyboard = [
        [InlineKeyboardButton("购买权限", callback_data=CALLBACK_PERMISSION_MENU)],
        [InlineKeyboardButton("购买记忆上限 +1 - 100金币", callback_data=CALLBACK_MEMORY_LIMIT)],
        [InlineKeyboardButton("购买彩票", callback_data=CALLBACK_LOTTERY_MENU)],
        [InlineKeyboardButton("关闭商店", callback_data=CALLBACK_CLOSE)],
    ]
    return Menu("欢迎来到商城，请选择购买项目：", InlineKeyboardMarkup(keyboard))


def permission_menu() -> Menu:
    keyboard = [
        [InlineKeyboardButton("升级权限等级到1级 - 50金币", callback_data="shop_upgrade_1")],
        [InlineKeyboardButton("升级权限等级到2级 - 100金币", callback_data="shop_upgrade_2")],
        [InlineKeyboardButton("升级权限等级到3级 - 10000金币", callback_data="shop_upgrade_3")],
        [InlineKeyboardButton("返回", callback_data=CALLBACK_HOME)],
    ]
    return Menu("请选择购买的项目：", InlineKeyboardMarkup(keyboard))


def lottery_menu() -> Menu:
    keyboard = [
        [InlineKeyboardButton("购买刮刮乐 - 10金币", callback_data=CALLBACK_SCRATCH)],
        [InlineKeyboardButton("购买欢乐彩 - 1金币", callback_data=CALLBACK_HUANLE)],
        [InlineKeyboardButton("返回", callback_data=CALLBACK_HOME)],
    ]
    return Menu("请选择购彩项目：", InlineKeyboardMarkup(keyboard))


# ---------------------------------------------------------------------------
# 购买结果的文案
# ---------------------------------------------------------------------------


def memory_limit_message(result: shop_purchases.MemoryLimitResult) -> str:
    if result.status is PurchaseStatus.NOT_REGISTERED:
        return NOT_REGISTERED_MESSAGE
    if result.status is PurchaseStatus.INSUFFICIENT:
        return INSUFFICIENT_MESSAGE
    new_limit = "?" if result.new_limit is None else result.new_limit
    return f"购买成功！永久记忆上限已提升至 {new_limit} 条。"


def permission_message(result: shop_purchases.PermissionUpgradeResult) -> str:
    if result.status is PurchaseStatus.NOT_REGISTERED:
        return NOT_REGISTERED_MESSAGE
    if result.status is PurchaseStatus.NOT_ELIGIBLE:
        assert result.refusal is not None
        return _UPGRADE_REFUSAL_TEXT[result.refusal]
    if result.status is PurchaseStatus.INSUFFICIENT:
        return INSUFFICIENT_MESSAGE
    return f"购买成功！您的权限已升级到{result.level}级。"


@dataclass(frozen=True, slots=True)
class TicketView:
    """一种彩票的展示用字段。"""

    game_name: str
    consolation_text: str  # 触发保底奖励时附加的说明


SCRATCH_VIEW = TicketView(
    game_name="刮刮乐",
    consolation_text="由于您连续5次都没抽到10个以上的金币，系统赠送您10个金币作为安慰！",
)
HUANLE_VIEW = TicketView(
    game_name="欢乐彩",
    consolation_text="由于您连续5次都没有获得奖励，系统赠送您2个金币作为安慰！",
)


def ticket_message(result: shop_purchases.TicketResult, view: TicketView) -> str:
    """购买结果的弹窗文案：拒绝原因，或开奖结果（触发保底时附带安慰说明）。"""
    if result.status is PurchaseStatus.NOT_REGISTERED:
        return NOT_REGISTERED_MESSAGE
    if result.status is PurchaseStatus.INSUFFICIENT:
        return f"硬币不足，您当前只有 {result.balance_total} 个硬币。"
    message = f"恭喜！您获得了 {result.reward} 个金币。"
    if result.bonus:
        message += f"\n\n{view.consolation_text}"
    return message


def lottery_record_line(user_label: str, view: TicketView, result: shop_purchases.TicketResult) -> str:
    """聊天里「最近的彩票记录」的一行。"""
    line = f"{user_label}: {view.game_name} → {result.reward}金币"
    if result.bonus:
        line += f" (触发保底奖励{result.bonus}金币!)"
    return line
