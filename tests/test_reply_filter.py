from features.ai.reply_filter import normalize_ai_reply_text


def test_no_response_sentinel_returns_empty_text():
    assert normalize_ai_reply_text("  [NO_RESPONSE]  ") == ""


def test_no_response_sentinel_is_removed_from_regular_text():
    assert normalize_ai_reply_text("hello [NO_RESPONSE] world") == "hello  world"


def test_multiple_no_response_sentinels_are_removed():
    assert normalize_ai_reply_text("[no_response]hello[NO_RESPONSE]") == "hello"


def test_regular_text_is_preserved():
    assert normalize_ai_reply_text("hello\nworld") == "hello\nworld"


def test_none_becomes_empty_text():
    assert normalize_ai_reply_text(None) == ""
