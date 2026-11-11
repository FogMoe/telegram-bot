"""AI 代执行 Telegram 命令（`execute_telegram_command`）时禁止的命令。

注册过的命令默认都能代执行，只禁「好处流向第三方」的几种：`/give`（把硬币转给别人）、带参数的
`/ref` 与 `/start`（绑定邀请人，邀请人拿到奖励）、带参数的 `/swap`（把硬币兑换到指定的钱包地址）。
模型会读到群里别人发的消息、网页正文和搜索结果，只有这几种值得别人诱导模型去执行；硬币可以充值购买，
损失是真钱。
"""

from __future__ import annotations

_REFERRAL_COMMANDS = frozenset({"ref", "start"})


def delegated_command_block_reason(command: str, command_text: str) -> str | None:
    """这条命令不能代执行时返回给模型看的原因，可以执行时返回 None。

    `command` 是不带 `/` 的小写命令名，`command_text` 是完整命令文本。
    """
    has_args = len(command_text.split()) > 1
    if command == "give":
        return (
            "/give transfers the user's coins to someone else and cannot be executed on "
            "the user's behalf. Tell the user to run /give themselves in Telegram. "
            "Do not retry this tool."
        )
    if command in _REFERRAL_COMMANDS and has_args:
        return (
            f"/{command} with arguments binds a referrer and cannot be executed on the "
            f"user's behalf. You may run /{command} without arguments, or tell the user "
            f"to send the full /{command} command themselves in Telegram."
        )
    if command == "swap" and has_args:
        return (
            "/swap with arguments exchanges the user's coins to a wallet address and "
            "cannot be executed on the user's behalf. You may run /swap without arguments "
            "to show how it works, or tell the user to send the full /swap command "
            "themselves in Telegram."
        )
    return None


__all__ = ["delegated_command_block_reason"]
