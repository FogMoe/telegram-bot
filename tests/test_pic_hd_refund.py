"""锚定 /pic 高清回调的金币语义。

这条路径先扣币再下载，失败时要退回去。几种结局必须固定：
拿到图或拿到备用链接就不退，两者都失败退且只退原来那一笔，还没扣就返回（余额不足、
扣费本身出错）的一分不动、也不交付任何内容。
"""

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from core import balance
from features.media import pic


class _FailingSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    def get(self, *args, **kwargs):
        raise RuntimeError("下载失败")


@pytest.fixture
def coin_calls(monkeypatch):
    """替换余额服务与奖池，返回记录下来的每一次调用。"""

    calls = []

    async def fake_debit(user_id, amount, *, op_key, reason, ref=None):
        calls.append(("debit", amount, op_key))
        return SimpleNamespace(applied=True)

    async def fake_refund(original_op_key, *, reason="refund"):
        calls.append(("refund", original_op_key))
        return SimpleNamespace(applied=True)

    async def fake_pool(cost, *, spend_op_key, reason="spend_share"):
        calls.append(("pool", cost, spend_op_key))

    async def fake_get_coins(user_id):
        return 100

    monkeypatch.setattr(pic.balance, "debit_standalone", fake_debit)
    monkeypatch.setattr(pic.balance, "refund_standalone", fake_refund)
    monkeypatch.setattr(pic.stake_reward_pool, "credit_share_of_spend_standalone", fake_pool)
    monkeypatch.setattr(pic.process_user, "async_get_user_coins", fake_get_coins)
    monkeypatch.setattr(
        pic.aiohttp,
        "ClientSession",
        lambda *args, **kwargs: _FailingSession(),
    )
    return calls


@pytest.fixture(autouse=True)
def _clean_module_state():
    pic.PROCESSING_IMAGES.clear()
    pic.HD_IMAGE_CACHE.clear()
    yield
    pic.PROCESSING_IMAGES.clear()
    pic.HD_IMAGE_CACHE.clear()


def _cache_image(image_id="abc"):
    pic.HD_IMAGE_CACHE[image_id] = {
        "file_url": "https://example.invalid/full.png",
        "expires": datetime.now() + timedelta(hours=1),
        "stats": {"file_size": 1024},
        "tags": "",
    }
    return image_id


def _build_update(image_id, *, fallback_send, query_id="query-1"):
    answers = []

    async def answer(text=None, show_alert=False):
        answers.append(text)

    async def edit_message_caption(caption=None, reply_markup=None):
        return None

    query = SimpleNamespace(
        id=query_id,
        data=f"pic_hd_{image_id}",
        answer=answer,
        edit_message_caption=edit_message_caption,
        message=SimpleNamespace(caption="高清原图 点击下方按钮 获取", message_id=42),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=123, username="kc"),
        effective_chat=SimpleNamespace(id=456),
    )
    context = SimpleNamespace(
        bot=SimpleNamespace(send_message=fallback_send, send_document=fallback_send)
    )
    return update, context, answers


def test_download_and_fallback_link_both_fail_refunds_the_original_debit_once(coin_calls):
    image_id = _cache_image()

    async def always_fail(*args, **kwargs):
        raise RuntimeError("发送失败")

    update, context, _ = _build_update(image_id, fallback_send=always_fail)

    asyncio.run(pic.hd_pic_callback(update, context))

    assert [call[:2] for call in coin_calls] == [("debit", pic.HD_COIN_COST), ("refund", coin_calls[0][2])]
    assert coin_calls[0][2] == "pic_hd:query-1"
    # 交付失败：奖池不增加。
    assert not any(call[0] == "pool" for call in coin_calls)


def test_fallback_link_success_does_not_refund_and_feeds_the_pool(coin_calls):
    image_id = _cache_image()

    async def send_ok(*args, **kwargs):
        return SimpleNamespace(message_id=99)

    update, context, _ = _build_update(image_id, fallback_send=send_ok)

    asyncio.run(pic.hd_pic_callback(update, context))

    # 下载失败但用户拿到了备用链接，这一轮的金币不退，奖池照常入账。
    assert coin_calls == [
        ("debit", pic.HD_COIN_COST, "pic_hd:query-1"),
        ("pool", pic.HD_COIN_COST, "pic_hd:query-1"),
    ]


def test_insufficient_coins_returns_before_charging_or_delivering(coin_calls, monkeypatch):
    image_id = _cache_image()

    async def poor_debit(user_id, amount, *, op_key, reason, ref=None):
        raise balance.InsufficientBalance(user_id, amount, balance.UserBalances(0, 0))

    async def poor_user(user_id):
        return 0

    monkeypatch.setattr(pic.balance, "debit_standalone", poor_debit)
    monkeypatch.setattr(pic.process_user, "async_get_user_coins", poor_user)

    async def unused(*args, **kwargs):
        raise AssertionError("金币不足时不应发送任何内容")

    update, context, answers = _build_update(image_id, fallback_send=unused)

    asyncio.run(pic.hd_pic_callback(update, context))

    assert coin_calls == []
    # 提前返回的分支必须把图片放回可重试状态。
    assert image_id not in pic.PROCESSING_IMAGES
    assert any("金币不足" in (text or "") for text in answers)


def test_a_debit_error_delivers_nothing_and_refunds_nothing(coin_calls, monkeypatch):
    image_id = _cache_image()

    async def broken_debit(user_id, amount, *, op_key, reason, ref=None):
        raise RuntimeError("数据库不可用")

    monkeypatch.setattr(pic.balance, "debit_standalone", broken_debit)

    async def unused(*args, **kwargs):
        raise AssertionError("没有扣费成功就不能交付备用链接")

    update, context, answers = _build_update(image_id, fallback_send=unused)

    asyncio.run(pic.hd_pic_callback(update, context))

    assert coin_calls == []
    assert image_id not in pic.PROCESSING_IMAGES
    assert any("出错" in (text or "") for text in answers)
