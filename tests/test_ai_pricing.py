"""AI 定价单点声明与零余额存储策略测试。

注意：按 docs/testing-guidelines.md 约定，这里只对合成字符串测试占位符替换，
绝不读取或断言 `resources/` 下的提示词与文档内容。
"""

import pytest

from fogmoe_telegram_bot.core import ai_pricing, chat_records
from fogmoe_telegram_bot.core.chat_records import ZeroBalancePolicy, _resolve_zero_balance_policy
from fogmoe_telegram_bot.features.conversation import billing


@pytest.mark.parametrize(
    ("length", "expected"),
    [
        (0, 1),
        (100, 1),
        (101, 2),
        (500, 2),
        (501, 3),
        (1000, 3),
        (1001, 4),
        (2000, 4),
        (2001, 5),
        (4096, 5),
    ],
)
def test_chat_text_cost_tiers_match_billing_module(length: int, expected: int):
    assert ai_pricing.chat_text_cost(length) == expected
    assert billing.text_message_cost(length) == expected


def test_billing_constants_come_from_ai_pricing():
    assert billing.MEDIA_COST == ai_pricing.CHAT_MEDIA_COST == 5
    assert billing.MAX_TEXT_LENGTH == ai_pricing.CHAT_MAX_TEXT_LENGTH == 4096


@pytest.mark.parametrize(
    ("length", "expected"),
    [
        (1, 0),
        (500, 0),
        (501, 1),
        (1000, 1),
        (1001, 2),
        (2000, 2),
        (2001, 3),
        (3000, 3),
    ],
)
def test_translate_text_cost_tiers(length: int, expected: int):
    assert ai_pricing.translate_text_cost(length) == expected


def test_render_pricing_placeholders_substitutes_known_keys_and_leaves_unknowns():
    template = (
        "chat={{AI_CHAT_MIN_COINS}}-{{AI_CHAT_MAX_COINS}}, "
        "media={{AI_CHAT_MEDIA_COINS}}, "
        "limit={{AI_CHAT_MAX_TEXT_LENGTH}}, "
        "tl_free={{AI_TRANSLATE_FREE_MAX_LENGTH}}, "
        "tl_max={{AI_TRANSLATE_MAX_COINS}}/{{AI_TRANSLATE_MAX_TEXT_LENGTH}}, "
        "other={{UNKNOWN_KEY}}"
    )

    rendered = ai_pricing.render_pricing_placeholders(template)

    assert rendered == (
        "chat=1-5, media=5, limit=4096, tl_free=500, tl_max=3/3000, other={{UNKNOWN_KEY}}"
    )


def test_zero_balance_policy_resolves_explicit_and_legacy_flags():
    assert (
        _resolve_zero_balance_policy(None, allow_zero_balance=False, suspend_if_zero=False)
        is ZeroBalancePolicy.ENFORCE
    )
    assert (
        _resolve_zero_balance_policy(None, allow_zero_balance=True, suspend_if_zero=False)
        is ZeroBalancePolicy.ALLOW
    )
    assert (
        _resolve_zero_balance_policy(None, allow_zero_balance=False, suspend_if_zero=True)
        is ZeroBalancePolicy.SUSPEND_BOUNDARY
    )
    assert (
        _resolve_zero_balance_policy(
            ZeroBalancePolicy.ALLOW,
            allow_zero_balance=False,
            suspend_if_zero=False,
        )
        is ZeroBalancePolicy.ALLOW
    )

    with pytest.raises(ValueError):
        _resolve_zero_balance_policy(None, allow_zero_balance=True, suspend_if_zero=True)

    with pytest.raises(ValueError):
        _resolve_zero_balance_policy(
            ZeroBalancePolicy.ENFORCE,
            allow_zero_balance=True,
            suspend_if_zero=False,
        )


def test_zero_balance_policy_is_exported_from_chat_records():
    assert chat_records.ZeroBalancePolicy is ZeroBalancePolicy
