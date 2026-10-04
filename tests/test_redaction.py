import io
import logging

import pytest

from core import config, redaction


@pytest.mark.parametrize(
    "text",
    [
        "429 Client Error for url: https://serpapi.com/search?engine=google&q=cat&api_key=SECRETVALUE123",
        "GET https://example.test/data?Access_Token=SECRETVALUE123&page=2",
        "https://example.test/cb?x=1&signature=SECRETVALUE123",
        "https://example.test/?KEY=SECRETVALUE123",
        "https://example.test/?client_secret=SECRETVALUE123&ok=1",
        "headers={'Authorization': 'Bearer SECRETVALUE123'}",
        "Authorization: Bearer SECRETVALUE123",
        "bad request: Bearer abcdefghijklmnopqrstuvwxyz0123",
        "https://api.telegram.org/bot123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw/getMe",
        "token leaked 123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw end",
        "connect mysql+asyncmy://bot:SECRETVALUE123@db.local/app failed",
        '{"api_key": "SECRETVALUE123", "q": "cat"}',
        "OPENAI_API_KEY=SECRETVALUE123",
        "Cookie: session=SECRETVALUE123; theme=dark",
        "(asyncmy.errors.OperationalError) (1213, 'Deadlock')\n"
        "[SQL: SELECT 1 WHERE code = %s]\n[parameters: ('SECRETVALUE123',)]\n"
        "(Background on this error at: https://sqlalche.me/e/20/e3q8)",
    ],
)
def test_redact_text_hides_credentials(text):
    result = redaction.redact_text(text)

    assert "SECRETVALUE123" not in result
    assert "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw" not in result
    assert "abcdefghijklmnopqrstuvwxyz0123" not in result
    assert redaction.REDACTED in result
    assert redaction.redact_text(result) == result


def test_redact_text_keeps_harmless_urls_and_text():
    text = (
        "see https://example.test/search?q=cat&page=2 and total_tokens=500, "
        "usage: /charge <code>, basic information, user:pass is not a url"
    )

    assert redaction.redact_text(text) == text


def test_redact_text_replaces_known_runtime_secrets(monkeypatch):
    monkeypatch.setattr(config, "SERPAPI_API_KEY", "runtime serp/key 0001")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "short")

    result = redaction.redact_text(
        "provider said runtime serp/key 0001 is invalid; also short, "
        "url-encoded runtime%20serp%2Fkey%200001 and runtime+serp%2Fkey+0001"
    )

    assert "serp" not in result
    # 低于长度阈值的配置值不参与精确匹配，避免误伤普通词。
    assert "also short," in result


def test_redact_text_replaces_registered_and_caller_secrets():
    redaction.register_secret("registered-secret-value")
    try:
        result = redaction.redact_text(
            "a registered-secret-value b tiny-secret c",
            extra_secrets=["tiny-secret"],
        )
    finally:
        redaction.unregister_secret("registered-secret-value")

    assert "registered-secret-value" not in result
    assert "tiny-secret" not in result


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/charge top-secret", "/charge [redacted]"),
        ("/CHARGE@FogMoeBot top-secret", "/charge [redacted]"),
        ("/webpassword@FogMoeBot abc123 def456", "/webpassword [redacted]"),
        ("/webpassword\nabc123", "/webpassword [redacted]"),
        ("/charge:top-secret", "/charge [redacted]"),
        ("/charge", "/charge"),
        ("/charge@FogMoeBot", "/charge"),
        ("/create_code 5 100", "/create_code 5 100"),
        ("/help me", "/help me"),
        ("charge top-secret", "charge top-secret"),
    ],
)
def test_redact_command_text(text, expected):
    assert redaction.redact_command_text(text) == expected


def test_command_secret_values_cover_arguments_and_tokens():
    values = redaction.command_secret_values("/charge@Bot abc-1234 zz")

    assert "abc-1234 zz" in values
    assert "abc-1234" in values
    assert "zz" not in values
    assert redaction.command_secret_values("/charge") == ()
    assert redaction.command_secret_values("/help abc") == ()


def test_redact_text_hides_sensitive_command_in_tool_arguments():
    arguments = '{"command": "/charge 123e4567-e89b-12d3-a456-426614174000"}'

    result = redaction.redact_text(arguments)

    assert "123e4567" not in result
    assert result == '{"command": "/charge [redacted]"}'


def test_redact_output_replaces_whole_reply_for_sensitive_output_commands():
    assert (
        redaction.redact_output("codes: abc", command="create_code")
        == redaction.SENSITIVE_OUTPUT_PLACEHOLDER
    )
    assert redaction.redact_output("codes: abc", command="help") == "codes: abc"


def test_mask_secret_keeps_only_tail():
    assert redaction.mask_secret("123e4567-e89b-12d3-a456-426614174000") == "****4000"
    assert redaction.mask_secret("short") == "****"


def test_describe_exception_has_type_redacted_and_truncated_summary():
    error = RuntimeError(
        "boom for https://x.test/?api_key=SECRETVALUE123 " + "filler " * 100
    )

    description = redaction.describe_exception(error, limit=80)

    assert description.startswith("RuntimeError: boom for")
    assert "SECRETVALUE123" not in description
    assert len(description) <= len("RuntimeError: ") + 80


def test_describe_exception_includes_http_status_when_available():
    class _HttpError(Exception):
        status_code = 429

    assert redaction.describe_exception(_HttpError("Too Many Requests")).endswith(
        "(HTTP 429)"
    )


def test_log_exception_logs_redacted_detail_with_same_reference(caplog):
    logger = logging.getLogger("tests.redaction")
    try:
        raise ValueError("failed: https://x.test/?token=SECRETVALUE123")
    except ValueError as exc:
        with caplog.at_level(logging.ERROR, logger="tests.redaction"):
            ref = redaction.log_exception(logger, "step failed", exc)

    assert ref.startswith("ERR-")
    assert f"[ref={ref}]" in caplog.text
    assert "ValueError" in caplog.text
    assert "SECRETVALUE123" not in caplog.text


def _logger_with_filter():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    handler.addFilter(redaction.RedactingFilter())
    logger = logging.getLogger(f"tests.redaction.filter.{id(stream)}")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.addHandler(handler)
    return logger, stream


def test_logging_filter_redacts_formatted_arguments():
    logger, stream = _logger_with_filter()

    logger.error("request failed: %s status=%d", "https://x.test/?api_key=SECRETVALUE123", 429)

    output = stream.getvalue()
    assert "SECRETVALUE123" not in output
    assert "api_key=[redacted]" in output
    assert "status=429" in output


def test_logging_filter_redacts_traceback_text():
    logger, stream = _logger_with_filter()

    try:
        raise RuntimeError("bad url https://x.test/?key=SECRETVALUE123")
    except RuntimeError:
        logger.exception("call failed")

    output = stream.getvalue()
    assert "call failed" in output
    assert "RuntimeError" in output
    assert "Traceback" in output
    assert "SECRETVALUE123" not in output


def test_logging_filter_does_not_swallow_malformed_log_calls():
    record = logging.LogRecord(
        "tests", logging.ERROR, __file__, 1, "needs two %s %s", ("one",), None
    )

    assert redaction.RedactingFilter().filter(record) is True
    assert record.msg == "needs two %s %s"


def test_logging_filter_is_shared_by_every_handler(tmp_path, monkeypatch):
    from core import bot_logging

    monkeypatch.setattr(config, "LOG_DIR", tmp_path)
    monkeypatch.setattr(config, "LOG_FILE_PATH", tmp_path / "bot.log")
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    for handler in saved_handlers:
        root.removeHandler(handler)
    try:
        bot_logging.configure_logging()
        logging.getLogger("tests.bot_logging").error(
            "failed %s", "https://x.test/?password=SECRETVALUE123"
        )
        for handler in root.handlers:
            handler.flush()
        log_text = (tmp_path / "bot.log").read_text(encoding="utf-8")
        assert all(
            any(isinstance(f, redaction.RedactingFilter) for f in h.filters)
            for h in root.handlers
        )
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)

    assert "SECRETVALUE123" not in log_text
    assert "password=[redacted]" in log_text


@pytest.mark.parametrize(
    "text",
    [
        "A" * 500_000,
        "[parameters: " * 20_000,
        "a://b:" * 50_000,
        "?a=b&" * 50_000,
    ],
    ids=["long-alnum", "unclosed-sql-parameters", "repeated-scheme", "many-params"],
)
def test_redact_text_stays_fast_on_pathological_input(text):
    import time

    started = time.perf_counter()
    redaction.redact_text(text)

    assert time.perf_counter() - started < 3
