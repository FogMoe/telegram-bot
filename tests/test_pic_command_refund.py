"""锚定 /pic 命令的金币语义：扣费之后、图片送达之前的任何失败都退款，送达之后的收尾失败不退。"""

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from fogmoe_telegram_bot.features.media import pic

USER_ID = 123
CHAT_ID = 456
MESSAGE_ID = 42


class Calls(list):
    def names(self):
        return [call[0] for call in self]


@pytest.fixture
def calls(monkeypatch):
    """替换余额服务、奖池与取图，返回记录下来的每一次调用。"""
    recorded = Calls()

    async def fake_debit(user_id, amount, *, op_key, reason, ref=None):
        recorded.append(("debit", op_key))
        return SimpleNamespace(applied=True)

    async def fake_refund(original_op_key, *, reason="refund"):
        recorded.append(("refund", original_op_key))
        return SimpleNamespace(applied=True)

    async def fake_pool(cost, *, spend_op_key, reason="spend_share"):
        recorded.append(("pool", spend_op_key))

    async def registered(user_id):
        return True

    async def fake_image(is_nsfw=False, user_id=None):
        recorded.append(("fetch",))
        return {"id": "img-1", "sample_url": "https://example.invalid/s.png"}

    monkeypatch.setattr(pic.balance, "debit_standalone", fake_debit)
    monkeypatch.setattr(pic.balance, "refund_standalone", fake_refund)
    monkeypatch.setattr(pic.stake_reward_pool, "credit_share_of_spend_standalone", fake_pool)
    monkeypatch.setattr(pic.process_user, "async_user_exists", registered)
    monkeypatch.setattr(pic, "get_random_image", fake_image)
    return recorded


@pytest.fixture(autouse=True)
def _clean_module_state():
    saved = {name: dict(getattr(pic, name)) for name in ("USER_HELP_RECORDS", "HD_IMAGE_CACHE")}
    pic.USER_HELP_RECORDS[USER_ID] = datetime.now()  # 跳过首次使用的帮助
    yield
    for name, value in saved.items():
        getattr(pic, name).clear()
        getattr(pic, name).update(value)
    pic.IMAGE_REQUESTERS.pop("img-1", None)
    pic.RECENT_SENT_IMAGES.discard("img-1")
    pic.USER_RECENT_IMAGES.pop(USER_ID, None)


class ProcessingMessage:
    def __init__(self, *, delete_error=None):
        self.edits = []
        self._delete_error = delete_error

    async def edit_text(self, text):
        self.edits.append(text)

    async def delete(self):
        if self._delete_error is not None:
            raise self._delete_error


def build(*, reply_results, send_photo):
    """`reply_results` 依次是每次 reply_text 的结果；异常实例表示那一次发送失败。"""
    replies = []
    results = list(reply_results)

    async def reply_text(text, **kwargs):
        replies.append(text)
        result = results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=USER_ID, username="kc"),
        effective_chat=SimpleNamespace(id=CHAT_ID),
        message=SimpleNamespace(message_id=MESSAGE_ID, reply_text=reply_text),
    )
    context = SimpleNamespace(args=[], bot=SimpleNamespace(send_photo=send_photo))
    return update, context, replies


async def delivered_photo(**kwargs):
    return SimpleNamespace(message_id=99)


def test_a_processing_notice_that_cannot_be_sent_refunds_the_charge(calls):
    update, context, replies = build(
        reply_results=[RuntimeError("Telegram 限流"), None],
        send_photo=delivered_photo,
    )

    asyncio.run(pic.pic_command.__wrapped__(update, context))

    debit_key = calls[0][1]
    assert calls == [("debit", debit_key), ("refund", debit_key)]  # 没有取图，也没有贡献奖池
    assert "金币已退还" in replies[-1]


def test_a_photo_that_cannot_be_sent_refunds_and_says_so_on_the_processing_message(calls):
    processing = ProcessingMessage()

    async def failing_photo(**kwargs):
        raise RuntimeError("发送失败")

    update, context, _ = build(reply_results=[processing], send_photo=failing_photo)

    asyncio.run(pic.pic_command.__wrapped__(update, context))

    assert calls.names() == ["debit", "fetch", "refund"]
    assert "金币已退还" in processing.edits[-1]


def test_cleanup_failing_after_the_photo_was_delivered_does_not_refund(calls):
    processing = ProcessingMessage(delete_error=RuntimeError("消息已被删除"))
    update, context, _ = build(reply_results=[processing], send_photo=delivered_photo)

    asyncio.run(pic.pic_command.__wrapped__(update, context))

    assert calls.names() == ["debit", "fetch", "pool"]
    assert processing.edits == []
