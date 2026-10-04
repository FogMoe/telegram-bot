"""翻译 /tl 与图片 /pic（含高清）：扣费 -> 交付 -> 失败退款，奖池只在交付成功后入账（真实 MySQL）。"""

from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from economy_support import (
    Recorder,
    ledger_rows,
    make_callback_update,
    make_command_update,
    make_context,
    pool_balance,
    pool_rows,
    seed_user,
    user_state,
)
from mysql_support import run

from features.ai import translate_handlers
from features.media import pic

# 绕过命令冷却装饰器（进程内状态），直接调用被装饰的函数。
tl_command = translate_handlers.tl_command.__wrapped__
pic_command = pic.pic_command.__wrapped__

LONG_TEXT = "字" * 600  # 501-1000 字符：1 个币


def kinds(url, user_id=1):
    return [(row["op_key"], row["kind"]) for row in ledger_rows(url, user_id)]


class TestTranslate:
    @pytest.fixture
    def translate(self, monkeypatch):
        calls = Recorder(result="translated")
        monkeypatch.setattr(translate_handlers.ai_chat, "translate_text", calls)
        return calls

    def run_tl(self, *, text=LONG_TEXT, message_id=5, reply_text=None):
        update = make_command_update(user_id=1, message_id=message_id, reply_text=reply_text)
        context = make_context()
        context.args = [text]
        run(tl_command(update, context))
        return update

    def test_success_charges_once_and_feeds_the_pool(self, app_database, translate):
        seed_user(app_database, 1, free=5)

        update = self.run_tl()

        assert update.message.reply_text.texts == ["translated"]
        assert user_state(app_database, 1)["free"] == 4
        assert kinds(app_database) == [("tl:1:5", "debit")]
        assert pool_balance(app_database) == Decimal("0.20")
        assert [row["op_key"] for row in pool_rows(app_database)] == ["pool:tl:1:5"]

    def test_the_same_command_delivered_twice_is_charged_once(self, app_database, translate):
        seed_user(app_database, 1, free=5)

        self.run_tl(message_id=5)
        self.run_tl(message_id=5)

        assert user_state(app_database, 1)["free"] == 4
        assert pool_balance(app_database) == Decimal("0.20")

    def test_a_translation_failure_refunds_the_debit_and_leaves_the_pool_alone(
        self, app_database, monkeypatch
    ):
        seed_user(app_database, 1, free=5)
        monkeypatch.setattr(
            translate_handlers.ai_chat,
            "translate_text",
            Recorder(fail_times=1, error=RuntimeError("provider down")),
        )

        update = self.run_tl()

        assert user_state(app_database, 1)["free"] == 5
        assert kinds(app_database) == [("tl:1:5", "debit"), ("refund:tl:1:5", "refund")]
        assert pool_balance(app_database) == Decimal("0")
        assert pool_rows(app_database) == []
        assert "refunded" in update.message.reply_text.texts[-1]

    def test_a_failure_to_send_the_translation_also_refunds(self, app_database, translate):
        seed_user(app_database, 1, free=5)

        self.run_tl(reply_text=Recorder(fail_times=1))

        assert user_state(app_database, 1)["free"] == 5
        assert kinds(app_database) == [("tl:1:5", "debit"), ("refund:tl:1:5", "refund")]
        assert pool_balance(app_database) == Decimal("0")

    def test_an_empty_wallet_does_not_translate(self, app_database, translate):
        seed_user(app_database, 1, free=0)

        update = self.run_tl()

        assert translate.calls == []
        assert "硬币不足" in update.message.reply_text.texts[-1]
        assert ledger_rows(app_database) == []
        assert pool_balance(app_database) == Decimal("0")

    def test_short_texts_are_free_and_leave_no_trace(self, app_database, translate):
        seed_user(app_database, 1, free=0)

        update = self.run_tl(text="hello")

        assert update.message.reply_text.texts == ["translated"]
        assert ledger_rows(app_database) == []
        assert pool_rows(app_database) == []

    def test_a_refund_failure_is_reported_honestly(self, app_database, monkeypatch):
        seed_user(app_database, 1, free=5)
        monkeypatch.setattr(
            translate_handlers.ai_chat,
            "translate_text",
            Recorder(fail_times=1, error=RuntimeError("provider down")),
        )

        async def broken_refund(*args, **kwargs):
            raise RuntimeError("数据库不可用")

        monkeypatch.setattr(translate_handlers.balance, "refund_standalone", broken_refund)

        update = self.run_tl()

        assert "refunded" not in update.message.reply_text.texts[-1]


class TestPicCommand:
    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch):
        self.monkeypatch = monkeypatch
        pic.USER_HELP_RECORDS[1] = datetime.now()  # 跳过首次使用的帮助页
        yield
        pic.USER_HELP_RECORDS.pop(1, None)

    def run_pic(self, *, image=None, send_photo=None, message_id=6):
        processing = SimpleNamespace(edit_text=Recorder(), delete=Recorder(), message_id=99)
        update = make_command_update(
            user_id=1, message_id=message_id, reply_text=Recorder(result=processing)
        )
        context = make_context(send_photo=send_photo or Recorder(result=SimpleNamespace(message_id=77)))
        context.args = []

        async def fake_image(is_nsfw, user_id):
            return image

        self.monkeypatch.setattr(pic, "get_random_image", fake_image)
        run(pic_command(update, context))
        return update, processing, context

    IMAGE = {"id": "img-1", "sample_url": "https://example.invalid/s.jpg", "file_url": None}

    def test_success_charges_once_and_feeds_the_pool(self, app_database):
        seed_user(app_database, 1, free=3, paid=10)

        _, processing, context = self.run_pic(image=dict(self.IMAGE))

        assert len(context.bot.send_photo.calls) == 1
        assert user_state(app_database, 1) == {"free": 0, "paid": 8, "plan": "paid"}
        assert kinds(app_database) == [("pic:1:6", "debit")]
        assert pool_balance(app_database) == Decimal("1.00")
        assert [row["op_key"] for row in pool_rows(app_database)] == ["pool:pic:1:6"]

    def test_no_image_refunds_and_leaves_the_pool_alone(self, app_database):
        seed_user(app_database, 1, free=3, paid=10)

        _, processing, _ = self.run_pic(image=None)

        assert user_state(app_database, 1) == {"free": 3, "paid": 10, "plan": "paid"}
        assert kinds(app_database) == [("pic:1:6", "debit"), ("refund:pic:1:6", "refund")]
        assert pool_balance(app_database) == Decimal("0")
        assert "已退还" in processing.edit_text.texts[-1]

    def test_a_failure_to_send_the_photo_refunds_along_the_original_route(self, app_database):
        seed_user(app_database, 1, free=3, paid=10)

        _, processing, _ = self.run_pic(
            image=dict(self.IMAGE), send_photo=Recorder(fail_times=1)
        )

        assert user_state(app_database, 1) == {"free": 3, "paid": 10, "plan": "paid"}
        assert kinds(app_database) == [("pic:1:6", "debit"), ("refund:pic:1:6", "refund")]
        assert pool_balance(app_database) == Decimal("0")
        assert pool_rows(app_database) == []

    def test_an_empty_wallet_neither_charges_nor_fetches(self, app_database):
        seed_user(app_database, 1, free=2)

        update, processing, context = self.run_pic(image=dict(self.IMAGE))

        assert context.bot.send_photo.calls == []
        assert processing.edit_text.calls == []
        assert "金币不足" in update.message.reply_text.texts[-1]
        assert ledger_rows(app_database) == []
        assert user_state(app_database, 1)["free"] == 2

    def test_the_same_command_delivered_twice_is_charged_once(self, app_database):
        seed_user(app_database, 1, free=10)

        self.run_pic(image=dict(self.IMAGE), message_id=6)
        self.run_pic(image=dict(self.IMAGE), message_id=6)

        assert user_state(app_database, 1)["free"] == 5
        assert pool_balance(app_database) == Decimal("1.00")


class TestHdPic:
    @pytest.fixture(autouse=True)
    def _setup(self):
        pic.PROCESSING_IMAGES.clear()
        pic.HD_IMAGE_CACHE.clear()
        pic.HD_IMAGE_CACHE["abc"] = {
            "file_url": "https://example.invalid/full.png",
            "expires": datetime.now() + timedelta(hours=1),
            "stats": {"file_size": 1024},
            "tags": "",
        }
        yield
        pic.PROCESSING_IMAGES.clear()
        pic.HD_IMAGE_CACHE.clear()

    @pytest.fixture(autouse=True)
    def _download_fails(self, monkeypatch):
        class FailingSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                return False

            def get(self, *args, **kwargs):
                raise RuntimeError("下载失败")

        monkeypatch.setattr(pic.aiohttp, "ClientSession", lambda *a, **k: FailingSession())

    def click(self, send_message):
        update, answer, _ = make_callback_update(from_user_id=1, data="pic_hd_abc", query_id="cq-9")
        context = make_context(send_message=send_message, send_document=send_message)
        run(pic.hd_pic_callback(update, context))
        return answer

    def test_the_fallback_link_keeps_the_charge_and_feeds_the_pool(self, app_database):
        seed_user(app_database, 1, free=4, paid=20)

        self.click(Recorder(result=SimpleNamespace(message_id=1)))

        assert user_state(app_database, 1) == {"free": 0, "paid": 14, "plan": "paid"}
        assert kinds(app_database) == [("pic_hd:cq-9", "debit")]
        assert pool_balance(app_database) == Decimal("2.00")

    def test_total_delivery_failure_refunds_the_original_debit_once(self, app_database):
        seed_user(app_database, 1, free=4, paid=20)

        answer = self.click(Recorder(fail_times=99))

        assert user_state(app_database, 1) == {"free": 4, "paid": 20, "plan": "paid"}
        assert kinds(app_database) == [
            ("pic_hd:cq-9", "debit"),
            ("refund:pic_hd:cq-9", "refund"),
        ]
        assert pool_balance(app_database) == Decimal("0")
        assert pool_rows(app_database) == []
        assert any("已退还" in (call[0][0] if call[0] else "") for call in answer.calls)

    def test_an_empty_wallet_stops_before_charging_and_allows_a_retry(self, app_database):
        seed_user(app_database, 1, free=3)

        self.click(Recorder(fail_times=99))

        assert ledger_rows(app_database) == []
        assert "abc" not in pic.PROCESSING_IMAGES
