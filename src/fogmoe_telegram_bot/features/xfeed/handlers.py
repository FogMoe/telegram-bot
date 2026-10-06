"""/xfeed：群管理员绑定一个 X 账号，机器人定时把它的新帖子（原创与引用）以「全文引用 + 原链接」发到群里。

适配层：解析命令、检查群与管理员身份、把业务操作的结果映射成回复；轮询任务负责取帖与发送。
规则与扣费在 `operations.py`，数据源在 `source.py`。
"""

from __future__ import annotations

import asyncio
import html
import logging
import time
from collections import defaultdict
from collections.abc import Sequence

from telegram import LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, ChatMigrated, Forbidden, TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes

from fogmoe_telegram_bot.core import balance
from fogmoe_telegram_bot.core.command_cooldown import cooldown

from . import operations, source
from .operations import BindStatus
from .repositories.feeds import ActiveFeed
from .source import XAccountNotFound, XPost, XSourceError

logger = logging.getLogger(__name__)

# 帖子正文最多占多少（Telegram 按 UTF-16 码元计数，一条消息上限 4096），给标题和链接留出余量
TEXT_LIMIT = 3800
FETCH_CONCURRENCY = 4
POLL_MINUTES = operations.POLL_INTERVAL_SECONDS // 60
MAX_AGE_HOURS = operations.MAX_POST_AGE_SECONDS // 3600

USAGE = (
    f"X 账号同步：机器人每 {POLL_MINUTES} 分钟检查一次绑定账号的新帖子（原创与引用），把全文和原链接发到本群。\n\n"
    "/xfeed - 查看本群的同步状态\n"
    "/xfeed bind <X用户名> - 绑定或更换账号（管理员）\n"
    "/xfeed unbind - 暂停同步（管理员）\n\n"
    f"每个群首次开通需要 {operations.ACTIVATION_PRICE} 金币，由开通的管理员支付，只需支付一次，之后换绑免费。"
)


async def _reply(update: Update, text: str) -> None:
    if update.message:
        await update.message.reply_text(text)


async def _is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await context.bot.get_chat_member(update.effective_chat.id, update.effective_user.id)
    except TelegramError as exc:
        logger.warning("/xfeed 检查管理员身份失败: %s", exc)
        return False
    return member.status in ("administrator", "creator")


async def _show_status(update: Update) -> None:
    feed = await operations.get_group_feed(update.effective_chat.id)
    if feed is None:
        status = "本群尚未开通 X 同步。"
    elif not feed.enabled:
        status = f"本群已开通 X 同步，目前已暂停（上次同步的是 @{feed.handle}）。"
    else:
        status = f"本群正在同步 @{feed.handle} 的帖子。"
    await _reply(update, f"{status}\n\n{USAGE}")


async def _bind(update: Update, args: Sequence[str]) -> None:
    if not args:
        await _reply(update, "用法：/xfeed bind <X用户名>，例如 /xfeed bind elonmusk")
        return
    handle = source.normalize_handle(args[0])
    if handle is None:
        await _reply(update, "X 用户名格式不对：只能包含字母、数字和下划线，最长 15 个字符。")
        return

    try:
        async with source.new_session() as session:
            posts = await source.fetch_posts(session, handle)
    except XAccountNotFound:
        await _reply(update, f"找不到 X 账号 @{handle}，请检查用户名。")
        return
    except XSourceError as exc:
        logger.warning("/xfeed 绑定时获取 @%s 失败: %s", handle, exc)
        await _reply(update, "暂时无法获取 X 数据，请稍后再试。本次没有扣费。")
        return

    chat_id = update.effective_chat.id
    message_id = getattr(update.message, "message_id", None)
    op_key = (
        balance.new_op_key("xfeed:adhoc")
        if message_id is None
        else operations.activation_op_key(chat_id, message_id)
    )
    handle = operations.canonical_handle(posts, handle)
    request = operations.BindRequest(
        chat_id=chat_id,
        user_id=update.effective_user.id,
        handle=handle,
        latest_post_id=operations.latest_own_post_id(posts),
        op_key=op_key,
    )
    try:
        result = await operations.bind_feed(request)
    except Exception:
        logger.exception("/xfeed 绑定 @%s 失败（chat %s）", handle, chat_id)
        await _reply(update, "绑定时出现问题，请稍后再试。")
        return

    if result.status is BindStatus.NOT_REGISTERED:
        await _reply(update, "请先使用 /me 命令注册个人信息，再来开通。")
        return
    if result.status is BindStatus.INSUFFICIENT:
        await _reply(
            update,
            f"开通需要 {operations.ACTIVATION_PRICE} 金币，你当前只有 {result.balance_total} 枚。",
        )
        return

    lines = [f"已绑定 @{handle}，之后它的新帖子会同步到本群（每 {POLL_MINUTES} 分钟检查一次）。"]
    if result.charged:
        lines.append(f"已扣除 {operations.ACTIVATION_PRICE} 金币开通本群的 X 同步，之后换绑免费。")
    elif result.replaced_handle:
        lines.append(f"已停止同步 @{result.replaced_handle}。")
    elif result.resumed:
        lines.append(f"从上次的进度继续，暂停期间的帖子只补发 {MAX_AGE_HOURS} 小时内的。")
    await _reply(update, "\n".join(lines))


async def _unbind(update: Update) -> None:
    if await operations.unbind_feed(update.effective_chat.id):
        await _reply(update, "已暂停同步。之后再次绑定不需要付费，绑回同一个账号会从这次的进度继续。")
    else:
        await _reply(update, "本群当前没有在同步 X 账号。")


@cooldown
async def xfeed_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type not in ("group", "supergroup"):
        await _reply(update, "此命令只能在群组中使用。\nThis command can only be used in groups.")
        return

    args: Sequence[str] = context.args or []
    sub_command = args[0].lower() if args else ""
    if sub_command not in ("bind", "unbind"):
        await _show_status(update)
        return

    if not await _is_admin(update, context):
        await _reply(update, "只有群组管理员才能使用此命令。\nOnly group administrators can use this command.")
        return
    if sub_command == "bind":
        await _bind(update, args[1:])
    else:
        await _unbind(update)


# ---------------------------------------------------------------------------
# 轮询与发送
# ---------------------------------------------------------------------------


def _truncate(text: str, limit: int) -> str:
    """截到 `limit` 个 UTF-16 码元以内（Telegram 的计数方式），超出时以省略号结尾。"""
    if len(text.encode("utf-16-le")) // 2 <= limit:
        return text
    units = 0
    for index, char in enumerate(text):
        units += 2 if ord(char) > 0xFFFF else 1
        if units > limit - 1:  # 留一个码元给省略号
            return text[:index].rstrip() + "…"
    return text


def _body(post: XPost) -> str:
    text = _truncate(post.text.strip(), TEXT_LIMIT)
    if text:
        return text
    if post.has_video:
        return "[视频]"
    if post.has_photo:
        return "[图片]"
    return "（无文字）"


def format_post(post: XPost) -> str:
    """一条帖子的群消息（HTML）：谁发的、可展开的全文引用、原链接。"""
    if post.quoted_author:
        headline = (
            f"<b>@{html.escape(post.author)}</b> 引用了 @{html.escape(post.quoted_author)} 的帖子"
        )
    else:
        headline = f"<b>@{html.escape(post.author)}</b> 发布了新帖子"
    return (
        f"{headline}\n"
        f"<blockquote expandable>{html.escape(_body(post))}</blockquote>\n"
        f"{post.url}"
    )


async def _deliver(context: ContextTypes.DEFAULT_TYPE, feed: ActiveFeed, posts: list[XPost]) -> None:
    """把一个群还没同步的帖子按时间顺序发出去，记下发到哪一条。"""
    plan = operations.plan_delivery(posts, feed.last_seen_id, now=time.time())
    delivered_id = plan.skipped_up_to
    for post in plan.posts:
        try:
            await context.bot.send_message(
                feed.chat_id,
                format_post(post),
                parse_mode=ParseMode.HTML,
                # 正文里可能有别的链接，预览固定用这条帖子的链接
                link_preview_options=LinkPreviewOptions(url=post.url),
            )
        except ChatMigrated as exc:
            moved = await operations.move_feed(feed.chat_id, exc.new_chat_id)
            logger.info("X 同步：群 %s 升级为 %s，设置%s", feed.chat_id, exc.new_chat_id, "已迁移" if moved else "未迁移")
            break
        except Forbidden as exc:
            logger.info("X 同步：机器人无法在群 %s 发言，停止同步 @%s: %s", feed.chat_id, feed.handle, exc)
            await operations.stop_feed(feed)
            break
        except BadRequest as exc:
            reason = str(exc).lower()
            if "chat not found" in reason or "rights to send" in reason:
                # 群没了，或者机器人被禁言：暂停，管理员处理好之后重新绑定即可从进度继续。
                logger.info("X 同步：无法发到群 %s，停止同步 @%s: %s", feed.chat_id, feed.handle, exc)
                await operations.stop_feed(feed)
                break
            # 这一条发不出去（内容被拒），跳过它，免得每轮都重试同一条。
            logger.warning("X 同步：群 %s 的帖子 %s 发送失败，跳过: %s", feed.chat_id, post.id, exc)
        except TelegramError as exc:
            # 超时、限流等暂时性错误：这一轮停在这里，下一轮从没发出的那条继续。
            logger.warning("X 同步：群 %s 发送中断: %s", feed.chat_id, exc)
            break
        delivered_id = post.id
    if delivered_id is not None:
        await operations.record_delivered(feed, delivered_id)


async def _fetch_all(handles: Sequence[str]) -> dict[str, list[XPost]]:
    """每个账号取一次时间线；取不到的账号这一轮跳过。"""
    semaphore = asyncio.Semaphore(FETCH_CONCURRENCY)
    timelines: dict[str, list[XPost]] = {}

    async with source.new_session() as session:

        async def fetch(handle: str) -> None:
            async with semaphore:
                try:
                    timelines[handle.lower()] = await source.fetch_posts(session, handle)
                except XAccountNotFound:
                    logger.warning("X 同步：账号 @%s 不存在或已改名", handle)
                except XSourceError as exc:
                    logger.warning("X 同步：获取 @%s 失败: %s", handle, exc)

        await asyncio.gather(*(fetch(handle) for handle in handles))
    return timelines


async def poll_x_feeds_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        active = await operations.active_feeds()
    except Exception:
        logger.exception("X 同步：读取绑定列表失败")
        return
    if not active:
        return

    by_handle: dict[str, list[ActiveFeed]] = defaultdict(list)
    for feed in active:
        by_handle[feed.handle.lower()].append(feed)
    timelines = await _fetch_all([group[0].handle for group in by_handle.values()])

    for key, group in by_handle.items():
        posts = timelines.get(key)
        if posts is None:
            continue
        for feed in group:
            try:
                await _deliver(context, feed, posts)
            except Exception:
                logger.exception("X 同步：群 %s 同步 @%s 失败", feed.chat_id, feed.handle)


def setup_xfeed_handlers(application: Application) -> None:
    application.add_handler(CommandHandler("xfeed", xfeed_command))
    application.job_queue.run_repeating(
        poll_x_feeds_job, interval=operations.POLL_INTERVAL_SECONDS, first=60
    )
