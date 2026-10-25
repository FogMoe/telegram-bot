"""垃圾信息过滤的展示：帮助菜单与按钮，以及由业务结果生成的回复与警告文案。

只依赖 Telegram 的按钮类型和 `operations.spam_control` 的结果类型，不碰数据库、不发消息；
发送与编辑由适配层（`spam_control.py`）负责。
"""

from __future__ import annotations

import html

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from fogmoe_telegram_bot.features.moderation import spam_strikes
from fogmoe_telegram_bot.features.moderation.spam_ai import operations as spam_ai_operations

from .operations.spam_control import AddKeywordResult, AddKeywordStatus, DeleteKeywordResult
from .repositories.spam_keywords import SpamKeyword

# 「查看更多功能」按钮的回调数据；注册的 pattern 是 `^spam_help$`。
CALLBACK_HELP = "spam_help"

HELP_TEXT = (
    "🛡️ <b>垃圾信息过滤功能使用说明</b> 🛡️\n\n"
    "<b>基本命令：</b>\n"
    "/spam - 开启/关闭垃圾信息过滤\n"
    "/spam help - 显示此帮助信息\n\n"
    "<b>过滤设置：</b>\n"
    "/spam links on - 开启链接过滤\n"
    "/spam links off - 关闭链接过滤\n"
    "/spam mentions on - 开启@提及过滤\n"
    "/spam mentions off - 关闭@提及过滤\n\n"
    "<b>自定义垃圾词：</b>\n"
    "/spam list - 列出自定义垃圾词\n"
    "/spam add &lt;关键词&gt; - 添加自定义垃圾词\n"
    "/spam add //&lt;正则表达式&gt; - 添加正则表达式匹配\n"
    "/spam del &lt;关键词&gt; - 删除自定义垃圾词\n\n"
    "<b>AI 识别（付费）：</b>\n"
    "/spam ai - 查看 AI 识别状态\n"
    "/spam ai on - 开启 AI 识别\n"
    "/spam ai off - 暂停 AI 识别\n"
    f"/spam ai renew - 续费 {spam_ai_operations.PERIOD_DAYS} 天\n\n"
    "<b>注意事项：</b>\n"
    "• 启用链接过滤后，所有包含链接的消息将被自动删除\n"
    "• 启用@提及过滤后，所有包含@用户名的消息将被自动删除\n"
    "• 每个群组最多可设置10个自定义垃圾词\n"
    "• 有自定义垃圾词时，全局垃圾词库将不生效\n"
    "• 管理员发送的消息不会被检测\n"
    "• 图片和视频的说明文字也会被检测\n"
    f"• {spam_strikes.WINDOW_TEXT}因自定义垃圾词或 AI 识别被删除 {spam_strikes.STRIKES_TO_KICK} 条消息的成员会被移出群组，"
    f"{spam_strikes.BAN_SECONDS // 3600} 小时内无法重新加入（需要机器人有封禁成员的权限）；全局垃圾词库命中只警告\n"
    f"• AI 识别每 {spam_ai_operations.PERIOD_DAYS} 天 {spam_ai_operations.PERIOD_PRICE} 金币，"
    "由开通或续费的管理员支付，到期不会自动扣费\n"
    f"• 开启 AI 识别后，每位成员接下来在本群发的 {spam_ai_operations.CHECKED_MESSAGES_PER_MEMBER} 条消息会由 AI 检查，"
    "被检查的消息会发送给第三方 AI 服务判断\n"
    "• 使用前请确保机器人有删除消息的权限"
)

# HTML 发送失败时的纯文本版本
HELP_TEXT_PLAIN = (
    "垃圾信息过滤功能命令说明：\n\n"
    "基本命令：\n"
    "/spam - 开启/关闭垃圾信息过滤\n"
    "/spam help - 显示此帮助信息\n\n"
    "过滤设置：\n"
    "/spam links on/off - 开启/关闭链接过滤\n"
    "/spam mentions on/off - 开启/关闭@提及过滤\n\n"
    "自定义垃圾词：\n"
    "/spam list - 列出自定义垃圾词\n"
    "/spam add <词> - 添加垃圾词\n"
    "/spam del <词> - 删除垃圾词\n\n"
    "AI 识别（付费）：\n"
    "/spam ai - 查看 AI 识别状态\n"
    "/spam ai on/off - 开启/暂停 AI 识别\n"
    f"/spam ai renew - 续费 {spam_ai_operations.PERIOD_DAYS} 天\n"
)

LINKS_USAGE = "设置链接过滤的正确格式是：\n/spam links on - 开启链接过滤\n/spam links off - 关闭链接过滤"
MENTIONS_USAGE = (
    "设置@提及过滤的正确格式是：\n/spam mentions on - 开启@提及过滤\n/spam mentions off - 关闭@提及过滤"
)
ADD_USAGE = (
    "添加自定义垃圾词的正确格式是：\n"
    "/spam add <垃圾词>\n\n"
    "添加正则表达式格式：\n"
    "/spam add //正则表达式\n\n"
    "示例：\n"
    "/spam add 博彩\n"
    "/spam add //\\d+元.*充值"
)
DEL_USAGE = "删除自定义垃圾词的正确格式是：\n/spam del <垃圾词>"
BAD_OPTION = "参数错误。请使用 on 或 off。"

# Markdown
ENABLED_TEXT = "垃圾信息过滤功能已 ***开启***。我将自动删除可能的垃圾消息并发出警告。"
DISABLED_TEXT = "垃圾信息过滤功能已 ***关闭***。"


def help_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("查看更多功能 (管理员)", callback_data=CALLBACK_HELP)]])


def link_blocking_text(enabled: bool) -> str:
    status = "开启" if enabled else "关闭"
    return f"链接过滤功能已{status}。{'所有链接消息将被视为垃圾信息处理。' if enabled else ''}"


def mention_blocking_text(enabled: bool) -> str:
    status = "开启" if enabled else "关闭"
    return f"@mention过滤功能已{status}。{'所有包含@mention的消息将被自动删除。' if enabled else ''}"


_ADD_KEYWORD_TEXT = {
    AddKeywordStatus.ADDED: "已添加自定义垃圾词: '{keyword}'",
    AddKeywordStatus.UPDATED: "已更新自定义垃圾词: '{keyword}'",
    AddKeywordStatus.INVALID_REGEX: "无效的正则表达式: {keyword}",
    AddKeywordStatus.TOO_LONG: "垃圾词太长，请不要超过255个字符。",
    AddKeywordStatus.LIMIT_REACHED: "每个群组最多只能设置10个自定义垃圾词，请先删除一些再添加。",
}


def add_keyword_text(result: AddKeywordResult) -> str:
    return _ADD_KEYWORD_TEXT[result.status].format(keyword=result.keyword)


def delete_keyword_text(result: DeleteKeywordResult) -> str:
    if result.deleted:
        return f"已删除自定义垃圾词: '{result.keyword}'"
    return f"未找到自定义垃圾词: '{result.keyword}'"


def keyword_list_text(keywords: list[SpamKeyword]) -> str:
    if not keywords:
        return "当前群组没有设置任何自定义垃圾词。"
    lines = ["当前群组的自定义垃圾词列表：\n"]
    for idx, keyword in enumerate(keywords, 1):
        if keyword.is_regex:
            lines.append(f"{idx}. 正则: '//{keyword.keyword}'")
        else:
            lines.append(f"{idx}. 关键词: '{keyword.keyword}'")
    lines.append("\n使用 /spam add <垃圾词> 添加垃圾词")
    lines.append("使用 /spam del <垃圾词> 删除垃圾词")
    return "\n".join(lines)


def link_warning(sender_html: str, link: str, count: int) -> str:
    return (
        f"⚠️ 注意: {sender_html} 发送的消息包含链接 <tg-spoiler>{html.escape(link)}</tg-spoiler>，已被自动删除。\n"
        f"本群组禁止发送链接。这是第 {count} 次警告。"
    )


def mention_warning(sender_html: str, mention: str, count: int) -> str:
    return (
        f"⚠️ 注意: {sender_html} 发送的消息包含@提及 <tg-spoiler>{mention}</tg-spoiler>，已被自动删除。\n"
        f"本群组禁止@提及用户。这是第 {count} 次警告。"
    )


def spam_reason_html(word: str) -> str:
    """接在发送者后面的删除原因。"""
    return f"发送的消息包含垃圾内容 <tg-spoiler>{html.escape(word)}</tg-spoiler>"


def global_word_warning(sender_html: str, reason_html: str, count: int) -> str:
    return (
        f"⚠️ 注意: {sender_html} {reason_html}，已被自动删除。\n"
        f"这是第 {count} 次警告。持续发送垃圾信息可能导致被禁言或移出群组。"
    )
