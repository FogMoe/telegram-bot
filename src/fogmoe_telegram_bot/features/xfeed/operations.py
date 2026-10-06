"""群组同步 X 账号帖子的业务操作：开通扣费、绑定与解绑、轮询时选帖与推进进度。不依赖 Telegram。

规则（已确认的产品决定）：

- 每个群首次开通扣 `ACTIVATION_PRICE` 金币，由发起绑定的管理员支付；之后换绑、解绑后再绑定都免费。
  扣费之后不退款，数据源失效也不退。
- 一个群同时只绑定一个 X 账号。
- 只同步原创帖子与引用帖子，转帖与回复都不同步。
- 第一次绑定一个账号时以它当前最新的帖子为起点，之前的帖子不补发。解绑只暂停同步，账号与进度保留，
  绑回同一个账号时从原来的进度继续；换成别的账号时重新以最新的帖子为起点。
- 发布超过 `MAX_POST_AGE_SECONDS` 的帖子不补发，长时间故障或暂停之后不会刷屏。
- 一轮轮询里每个群最多发 `MAX_POSTS_PER_POLL` 条，超出的较早帖子跳过。

开通的扣款与开通记录在同一个事务里提交。`op_key` 以 `/xfeed bind` 命令消息为身份（`activation_op_key`），
同一条命令被重复投递时看到群已开通，按换绑处理，不会再扣一次。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_telegram_bot.core import balance, sql
from fogmoe_telegram_bot.core.command_identity import message_identity

from .repositories import feeds
from .repositories.feeds import ActiveFeed, GroupFeed
from .source import XPost

ACTIVATION_PRICE = 100
POLL_INTERVAL_SECONDS = 240
MAX_POSTS_PER_POLL = 5
MAX_POST_AGE_SECONDS = 24 * 3600


class BindStatus(StrEnum):
    BOUND = "bound"
    NOT_REGISTERED = "not_registered"
    INSUFFICIENT = "insufficient"


@dataclass(frozen=True, slots=True)
class BindRequest:
    chat_id: int
    user_id: int
    handle: str
    latest_post_id: int  # 账号当前最新帖子的 id，作为同步起点；没有帖子时为 0
    op_key: str


@dataclass(frozen=True, slots=True)
class BindResult:
    status: BindStatus
    charged: bool = False  # BOUND：这一次是否扣了开通费
    replaced_handle: str | None = None  # BOUND：换绑时之前正在同步、现在停掉的账号
    resumed: bool = False  # BOUND：绑回了暂停中的同一个账号，从原来的进度继续
    balance_total: int = 0  # INSUFFICIENT：当前余额


@dataclass(frozen=True, slots=True)
class DeliveryPlan:
    posts: list[XPost]  # 要发的帖子，从旧到新
    skipped_up_to: int | None  # 不发但要越过的帖子（太旧或超出每轮上限）里最新的 id


def activation_op_key(chat_id: int, message_id: int) -> str:
    return balance.make_op_key("xfeed", *message_identity(chat_id, message_id))


def is_syncable(post: XPost) -> bool:
    """原创与引用同步；转帖与回复不同步。"""
    return not post.is_repost and not post.is_reply


def latest_own_post_id(posts: Iterable[XPost]) -> int:
    """账号自己发的帖子（含回复，不含转帖）里最新的 id，作为同步起点。

    转帖的 id 是原帖的，比转帖时间早，不能当起点。没有帖子时为 0。
    """
    return max((post.id for post in posts if not post.is_repost), default=0)


def canonical_handle(posts: Iterable[XPost], requested: str) -> str:
    """用帖子里作者的写法（大小写）作为账号名；拿不到时用输入的写法。"""
    for post in posts:
        if not post.is_repost and post.author.lower() == requested.lower():
            return post.author
    return requested


def plan_delivery(
    posts: Iterable[XPost],
    last_seen_id: int,
    *,
    now: float,
    limit: int = MAX_POSTS_PER_POLL,
    max_age: float = MAX_POST_AGE_SECONDS,
) -> DeliveryPlan:
    """比 `last_seen_id` 新、需要同步的帖子里，发布不超过 `max_age` 秒的最新 `limit` 条，从旧到新。

    其余的新帖子不发，但进度要越过它们。帖子 id 随发布时间递增，被跳过的帖子都比要发的早，
    所以先把进度推到 `skipped_up_to`，再随每条发出的帖子前进，中途失败也不会漏发或重发。
    """
    fresh = {post.id: post for post in posts if is_syncable(post) and post.id > last_seen_id}
    ordered = [fresh[post_id] for post_id in sorted(fresh)]
    recent = [post for post in ordered if post.created_at is None or now - post.created_at <= max_age]
    to_send = recent[-limit:] if limit > 0 else []
    sending = {post.id for post in to_send}
    skipped = [post.id for post in ordered if post.id not in sending]
    return DeliveryPlan(to_send, max(skipped, default=None))


async def get_group_feed(chat_id: int) -> GroupFeed | None:
    return await feeds.get_feed(chat_id)


async def bind_feed(request: BindRequest) -> BindResult:
    """绑定账号；群还没开通时先扣开通费。扣款与开通记录同一个事务。"""

    async def activate(connection: AsyncConnection) -> BindResult | None:
        """开通并绑定；群已经被别人先开通时返回 None，由调用方按换绑处理。"""
        try:
            balances = await balance.lock_user(connection, request.user_id)
        except balance.UserNotFound:
            return BindResult(BindStatus.NOT_REGISTERED)
        if balances.total < ACTIVATION_PRICE:
            return BindResult(BindStatus.INSUFFICIENT, balance_total=balances.total)
        inserted = await feeds.insert_feed(
            connection,
            chat_id=request.chat_id,
            handle=request.handle,
            last_seen_id=request.latest_post_id,
            user_id=request.user_id,
            op_key=request.op_key,
        )
        if not inserted:
            return None
        # user 行已经锁住并确认过余额，这里不会余额不足；万一抛出，整个事务回滚，开通记录一起撤销。
        await balance.debit(
            connection,
            request.user_id,
            ACTIVATION_PRICE,
            op_key=request.op_key,
            reason="xfeed_activation",
        )
        return BindResult(BindStatus.BOUND, charged=True)

    async def work(connection: AsyncConnection) -> BindResult:
        if await feeds.get_feed(request.chat_id, connection=connection) is None:
            activated = await activate(connection)
            if activated is not None:
                return activated
        feed = await feeds.get_feed(request.chat_id, connection=connection, for_update=True)
        if feed is None:
            # 行只会在群升级成超级群时被搬走；让管理员在新群里重试。
            raise LookupError(f"group_x_feeds 里找不到 chat {request.chat_id}")
        same_account = feed.handle.lower() == request.handle.lower()
        await feeds.set_binding(
            connection,
            request.chat_id,
            handle=request.handle,
            last_seen_id=feed.last_seen_id if same_account else request.latest_post_id,
            user_id=request.user_id,
        )
        return BindResult(
            BindStatus.BOUND,
            replaced_handle=feed.handle if feed.enabled and not same_account else None,
            resumed=same_account and not feed.enabled,
        )

    return await balance.run_in_transaction(work)


async def unbind_feed(chat_id: int) -> bool:
    """暂停同步，开通记录、账号与进度都保留；当前没有在同步时返回 False。"""
    async with sql.transaction() as connection:
        return await feeds.disable(connection, chat_id)


async def active_feeds() -> list[ActiveFeed]:
    return await feeds.list_active_feeds()


async def record_delivered(feed: ActiveFeed, last_seen_id: int) -> None:
    """记下这个群已经同步到的帖子；群在发送期间换绑了账号时不改动。"""
    async with sql.transaction() as connection:
        await feeds.advance_last_seen(
            connection, feed.chat_id, handle=feed.handle, last_seen_id=last_seen_id
        )


async def stop_feed(feed: ActiveFeed) -> None:
    """机器人不在群里了：暂停同步，重新拉进群后再绑定不收费。"""
    async with sql.transaction() as connection:
        await feeds.disable(connection, feed.chat_id, handle=feed.handle)


async def move_feed(old_chat_id: int, new_chat_id: int) -> bool:
    """群升级成超级群：设置跟着搬到新 chat id。新群已经有自己的记录时只暂停旧群，返回 False。"""
    async with sql.transaction() as connection:
        moved = await feeds.move_chat(connection, old_chat_id, new_chat_id)
        if not moved:
            await feeds.disable(connection, old_chat_id)
        return moved
