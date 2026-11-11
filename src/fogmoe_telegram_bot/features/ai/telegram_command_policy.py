"""AI 代执行 Telegram 命令（`execute_telegram_command`）的白名单与策略表。

每一个注册到 `Application` 的 Telegram 命令都必须在 `COMMAND_POLICIES` 里显式归类（由单测对照
`register_handlers` 校验完整性）：

- 允许代执行：只读查询、免费/无硬币支出的自助命令、只打开交互面板的命令（含仅在无参数时只读展示面板的
  `/stake`、`/btc_predict`、`/chart` 与只读子命令的 `/rpg`）；真正的押注、购买或质押仍由用户自己点按钮或发命令完成。
- 禁止代执行：转账（`/give`）、直接扣硬币或发起付费动作的命令、凭证与充值相关命令、群管命令、管理员命令，
  以及会再次进入 AI 对话入口的 `/fogmoebot`。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType


class CommandCategory(StrEnum):
    READ_ONLY = "read_only"
    SELF_SERVICE = "self_service"
    PANEL_WITHOUT_ARGS = "panel_without_args"
    RPG_READ_ONLY = "rpg_read_only"
    TRANSFERS_COINS = "transfers_coins"
    SPENDS_COINS = "spends_coins"
    CREDENTIAL = "credential"
    MODERATION = "moderation"
    ADMIN = "admin"
    CONVERSATION_TRIGGER = "conversation_trigger"


@dataclass(frozen=True, slots=True)
class CommandPolicy:
    category: CommandCategory
    allowed: bool
    allowed_subcommands: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class DelegatedCommandDecision:
    allowed: bool
    category: CommandCategory | None
    error_message: str | None = None


_ALLOW_READ_ONLY = CommandPolicy(CommandCategory.READ_ONLY, allowed=True)
_ALLOW_SELF_SERVICE = CommandPolicy(CommandCategory.SELF_SERVICE, allowed=True)
_ALLOW_PANEL_NO_ARGS = CommandPolicy(CommandCategory.PANEL_WITHOUT_ARGS, allowed=True)
_ALLOW_RPG_READ_ONLY = CommandPolicy(
    CommandCategory.RPG_READ_ONLY,
    allowed=True,
    allowed_subcommands=frozenset({"help", "monsters"}),
)
_BLOCK_TRANSFER = CommandPolicy(CommandCategory.TRANSFERS_COINS, allowed=False)
_BLOCK_SPEND = CommandPolicy(CommandCategory.SPENDS_COINS, allowed=False)
_BLOCK_CREDENTIAL = CommandPolicy(CommandCategory.CREDENTIAL, allowed=False)
_BLOCK_MODERATION = CommandPolicy(CommandCategory.MODERATION, allowed=False)
_BLOCK_ADMIN = CommandPolicy(CommandCategory.ADMIN, allowed=False)
_BLOCK_TRIGGER = CommandPolicy(CommandCategory.CONVERSATION_TRIGGER, allowed=False)

COMMAND_POLICIES: Mapping[str, CommandPolicy] = MappingProxyType(
    {
        # 只读查询
        "me": _ALLOW_READ_ONLY,
        "help": _ALLOW_READ_ONLY,
        "github": _ALLOW_READ_ONLY,
        "rich": _ALLOW_READ_ONLY,
        # 免费自助、交互面板打开与会话管理（不在命令本身扣除用户硬币）
        "start": _ALLOW_SELF_SERVICE,
        "lottery": _ALLOW_SELF_SERVICE,
        "checkin": _ALLOW_SELF_SERVICE,
        "clear": _ALLOW_SELF_SERVICE,
        "setmyinfo": _ALLOW_SELF_SERVICE,
        "music": _ALLOW_SELF_SERVICE,
        "ref": _ALLOW_SELF_SERVICE,
        "shop": _ALLOW_SELF_SERVICE,
        "task": _ALLOW_SELF_SERVICE,
        "gamble": _ALLOW_SELF_SERVICE,
        "sicbo": _ALLOW_SELF_SERVICE,
        # 无参数时仅展示状态或交互面板；带参数会直接质押、锁仓或修改群绑定
        "stake": _ALLOW_PANEL_NO_ARGS,
        "btc_predict": _ALLOW_PANEL_NO_ARGS,
        "chart": _ALLOW_PANEL_NO_ARGS,
        # /rpg 无参数展示角色状态，help/monsters 只读；battle/heal/equip/item 会产生战斗、扣费或改装备
        "rpg": _ALLOW_RPG_READ_ONLY,
        # 硬币转账与直接消费
        "give": _BLOCK_TRANSFER,
        "pic": _BLOCK_SPEND,
        "omikuji": _BLOCK_SPEND,
        "swap": _BLOCK_SPEND,
        "tl": _BLOCK_SPEND,
        "rps_game": _BLOCK_SPEND,
        # 凭证与充值
        "charge": _BLOCK_CREDENTIAL,
        "webpassword": _BLOCK_CREDENTIAL,
        "recharge": _BLOCK_CREDENTIAL,
        # 群组管理与举报
        "verify": _BLOCK_MODERATION,
        "keyword": _BLOCK_MODERATION,
        "spam": _BLOCK_MODERATION,
        "xfeed": _BLOCK_MODERATION,
        "report": _BLOCK_MODERATION,
        # 管理员命令
        "admin_announce": _BLOCK_ADMIN,
        "start_test_monitor": _BLOCK_ADMIN,
        "stop_test_monitor": _BLOCK_ADMIN,
        "create_code": _BLOCK_ADMIN,
        "stats": _BLOCK_ADMIN,
        "logs": _BLOCK_ADMIN,
        # 主对话触发命令（避免递归触发）
        "fogmoebot": _BLOCK_TRIGGER,
    }
)


def split_telegram_command(command_text: str) -> tuple[str, tuple[str, ...]]:
    """把 `/cmd@bot arg1 arg2` 拆成规范化命令名与参数元组。"""
    parts = command_text.strip().split()
    if not parts or not parts[0].startswith("/"):
        return "", ()
    raw_name = parts[0][1:].split("@", maxsplit=1)[0].strip().lower()
    return raw_name, tuple(parts[1:])


def _blocked_message(command: str, category: CommandCategory, *, with_args: bool = False) -> str:
    if category is CommandCategory.PANEL_WITHOUT_ARGS and with_args:
        return (
            f"/{command} with arguments cannot be executed on the user's behalf because it "
            f"modifies stakes, predictions, or bindings. You may run /{command} without "
            f"arguments to open its status/panel, or tell the user to run the parameterized "
            f"/{command} command themselves in Telegram."
        )
    if category is CommandCategory.RPG_READ_ONLY and with_args:
        return (
            "Only `/rpg`, `/rpg help`, and `/rpg monsters` can be executed on the user's "
            "behalf. Tell the user to run battle, heal, equipment, or item subcommands "
            "themselves in Telegram. Do not retry this tool."
        )
    if category in {CommandCategory.TRANSFERS_COINS, CommandCategory.SPENDS_COINS}:
        return (
            f"/{command} transfers or spends coins and cannot be executed on the user's "
            f"behalf. Tell the user to run /{command} themselves in Telegram. "
            "Do not retry this tool."
        )
    if category is CommandCategory.CREDENTIAL:
        return (
            f"/{command} handles sensitive credentials or top-ups and cannot be executed "
            f"on the user's behalf. Tell the user to run /{command} themselves in private "
            "chat. Do not retry this tool."
        )
    return (
        f"/{command} is not allowed for delegated AI execution. Tell the user to run "
        f"/{command} themselves in Telegram. Do not retry this tool."
    )


def evaluate_delegated_command(command_text: str) -> DelegatedCommandDecision:
    """判断一条完整命令文本是否允许由 AI 代执行。"""
    command, args = split_telegram_command(command_text)
    policy = COMMAND_POLICIES.get(command)
    if policy is None:
        return DelegatedCommandDecision(
            allowed=False,
            category=None,
            error_message=(
                f"/{command} is not on the delegated command allowlist. Tell the user to "
                f"run /{command} themselves in Telegram. Do not retry this tool."
            ),
        )

    if not policy.allowed:
        return DelegatedCommandDecision(
            allowed=False,
            category=policy.category,
            error_message=_blocked_message(command, policy.category),
        )

    if policy.category is CommandCategory.PANEL_WITHOUT_ARGS and args:
        return DelegatedCommandDecision(
            allowed=False,
            category=policy.category,
            error_message=_blocked_message(command, policy.category, with_args=True),
        )

    if policy.category is CommandCategory.RPG_READ_ONLY and args:
        if len(args) != 1 or args[0].lower() not in policy.allowed_subcommands:
            return DelegatedCommandDecision(
                allowed=False,
                category=policy.category,
                error_message=_blocked_message(command, policy.category, with_args=True),
            )

    return DelegatedCommandDecision(allowed=True, category=policy.category)


__all__ = [
    "COMMAND_POLICIES",
    "CommandCategory",
    "CommandPolicy",
    "DelegatedCommandDecision",
    "evaluate_delegated_command",
    "split_telegram_command",
]
