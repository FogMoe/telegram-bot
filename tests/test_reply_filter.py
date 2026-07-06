from features.ai.reply_filter import normalize_ai_reply_text


def test_no_response_sentinel_returns_empty_text():
    assert normalize_ai_reply_text("  [NO_RESPONSE]  ") == ""


def test_regular_text_is_preserved():
    assert normalize_ai_reply_text("hello\nworld") == "hello\nworld"


def test_none_becomes_empty_text():
    assert normalize_ai_reply_text(None) == ""
