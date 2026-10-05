"""敏感数据脱敏策略的单一来源。

个人历史、群聊历史、工具结果持久化、诊断日志、全局错误处理和用户可见的
错误回复都经由本模块的函数处理敏感内容，策略只在这里定义。覆盖路径与运维
注意事项见 docs/sensitive-data.md。
"""

from __future__ import annotations

import logging
import re
import secrets
import sys
import traceback
from collections.abc import Callable, Iterable
from urllib.parse import quote, quote_plus

REDACTED = "[redacted]"
SENSITIVE_OUTPUT_PLACEHOLDER = "[sensitive command output redacted]"

# 参数本身是凭据的命令：历史里只保留命令名。
SENSITIVE_ARGUMENT_COMMANDS = frozenset({"charge", "webpassword"})
# 回复内容本身是凭据的命令：历史里不保留回复正文。
SENSITIVE_OUTPUT_COMMANDS = frozenset({"create_code"})
# 只允许私聊使用的命令，由 core.command_privacy 统一拦截。
PRIVATE_ONLY_COMMANDS = SENSITIVE_ARGUMENT_COMMANDS | SENSITIVE_OUTPUT_COMMANDS

# 运行时已知密钥参与精确匹配的最小长度，避免短值误伤普通文本。
MIN_KNOWN_SECRET_LENGTH = 8
DEFAULT_SUMMARY_LIMIT = 200


# --- 命令文本 ---------------------------------------------------------------

# Telegram 命令实体：/name 或 /name@BotName，名称大小写不敏感。
_COMMAND_PREFIX = re.compile(r"/([A-Za-z0-9_]+)(?:@[A-Za-z0-9_]+)?")


def command_name(text: str | None) -> str | None:
    """返回消息开头命令的小写名称（忽略 @BotName），不是命令则返回 None。"""
    if not text:
        return None
    match = _COMMAND_PREFIX.match(text)
    return match.group(1).lower() if match else None


def has_sensitive_arguments(text: str | None) -> bool:
    """文本是否是带参数的凭据类命令，例如 ``/charge <卡密>``。"""
    if not text:
        return False
    match = _COMMAND_PREFIX.match(text)
    if not match or match.group(1).lower() not in SENSITIVE_ARGUMENT_COMMANDS:
        return False
    return bool(text[match.end():].strip())


def redact_command_text(text: str) -> str:
    """凭据类命令只保留命令名；其他文本原样返回。"""
    if not text:
        return text
    match = _COMMAND_PREFIX.match(text)
    if not match:
        return text
    name = match.group(1).lower()
    if name not in SENSITIVE_ARGUMENT_COMMANDS:
        return text
    return f"/{name} {REDACTED}" if text[match.end():].strip() else f"/{name}"


def command_secret_values(text: str | None) -> tuple[str, ...]:
    """返回凭据类命令的参数原值，用于从该命令的回复里把同一值替换掉。"""
    if not text or not has_sensitive_arguments(text):
        return ()
    match = _COMMAND_PREFIX.match(text)
    arguments = text[match.end():].strip() if match else ""
    values = [arguments]
    values.extend(token for token in arguments.split() if len(token) >= 4)
    return tuple(dict.fromkeys(values))


# --- 自由文本 ---------------------------------------------------------------

_TELEGRAM_BOT_TOKEN = re.compile(
    r"bot\d{5,}:[A-Za-z0-9_-]{30,}"
    r"|(?<![A-Za-z0-9_])\d{5,}:[A-Za-z0-9_-]{30,}(?![A-Za-z0-9_-])"
)

_BEARER_TOKEN = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{16,}")

_AUTH_HEADER = re.compile(
    r"(?i)([\"']?(?:proxy-)?authorization[\"']?\s*[:=]\s*[\"']?)"
    r"(?:(bearer|basic|token|digest)\s+)?([^\s\\\"',;}]+)"
)

_COOKIE_HEADER = re.compile(r"(?i)\b((?:set-)?cookie\s*:\s*)([^\r\n\\\"']+)")

_SECRET_KEY_VALUE = re.compile(
    r"(?i)([\"']?(?<![A-Za-z0-9])(?:x-api-key|x-goog-api-key|api[_-]?key|apikey"
    r"|access[_-]?token|refresh[_-]?token|auth[_-]?token|bot[_-]?token"
    r"|client[_-]?secret|private[_-]?key|secret|password|passwd)[\"']?\s*[:=]\s*[\"']?)"
    r"([^\s\\\"',;&}\]]+)"
)

# 前置的负向断言让长字母数字串（如 base64）只从串首尝试一次，避免退化为平方级回溯。
_URL_USERINFO = re.compile(
    r"(?i)(?<![a-z0-9+.-])([a-z][a-z0-9+.-]*://[^/\s:@]+:)([^/\s@]+)(@)"
)

# SQLAlchemy 异常文本会带上绑定参数（卡密、密码哈希等），整段替换。
_SQL_PARAMETERS_OPEN = "[parameters: "
_SQL_PARAMETERS_END = re.compile(r"\](?=\s*\(Background on this error|\s*\n|\s*\Z)")
_SQL_PARAMETERS_REPLACEMENT = f"{_SQL_PARAMETERS_OPEN}{REDACTED}]"

_URL_QUERY_PARAM = re.compile(r"([?&;])([A-Za-z0-9_.%-]+)=([^&#\s\\\"'<>]*)")

_SENSITIVE_PARAM_NAMES = frozenset(
    {
        "key",
        "apikey",
        "token",
        "auth",
        "authorization",
        "password",
        "passwd",
        "pwd",
        "secret",
        "sig",
        "signature",
        "credential",
        "session",
        "sessionid",
    }
)
_SENSITIVE_PARAM_SUFFIXES = (
    "_key",
    "_token",
    "_secret",
    "_password",
    "_signature",
    "_sig",
    "_credential",
)

# 日志、工具参数里出现的凭据类命令文本，例如 {"command": "/charge 卡密"}。
_SENSITIVE_COMMAND_TEXT = re.compile(
    r"(?i)(?<![\w/@.])(/(?:" + "|".join(sorted(SENSITIVE_ARGUMENT_COMMANDS)) + r")(?:@\w+)?)"
    r"[ \t]+(?![<\[])[^\r\n\\\"'}]+"
)


def _is_sensitive_param(name: str) -> bool:
    normalized = name.lower().replace("-", "_").replace(".", "_")
    return normalized in _SENSITIVE_PARAM_NAMES or normalized.endswith(
        _SENSITIVE_PARAM_SUFFIXES
    )


def _redact_query_param(match: re.Match[str]) -> str:
    separator, name, value = match.groups()
    if value == REDACTED or not _is_sensitive_param(name):
        return match.group(0)
    return f"{separator}{name}={REDACTED}"


def _redact_auth_header(match: re.Match[str]) -> str:
    prefix, scheme, token = match.groups()
    if token.startswith(REDACTED[:-1]):
        return match.group(0)
    return f"{prefix}{scheme + ' ' if scheme else ''}{REDACTED}"


def _redact_cookie_header(match: re.Match[str]) -> str:
    prefix, value = match.groups()
    return match.group(0) if value.strip() == REDACTED else f"{prefix}{REDACTED}"


def _redact_key_value(match: re.Match[str]) -> str:
    prefix, value = match.groups()
    if value.startswith(REDACTED[:-1]):
        return match.group(0)
    return f"{prefix}{REDACTED}"


def _redact_userinfo(match: re.Match[str]) -> str:
    prefix, password, at = match.groups()
    return match.group(0) if password == REDACTED else f"{prefix}{REDACTED}{at}"


def _redact_sql_parameters(value: str) -> str:
    """替换 SQLAlchemy 异常里的 ``[parameters: ...]``；找不到结尾时替换到文本末尾。

    不用单个正则：缺少结尾的重复开头会让回溯退化为平方级。
    """
    if _SQL_PARAMETERS_OPEN not in value:
        return value
    parts: list[str] = []
    position = 0
    while True:
        start = value.find(_SQL_PARAMETERS_OPEN, position)
        if start < 0:
            break
        parts.append(value[position:start])
        parts.append(_SQL_PARAMETERS_REPLACEMENT)
        end = _SQL_PARAMETERS_END.search(value, start + len(_SQL_PARAMETERS_OPEN))
        if end is None:
            position = len(value)
            break
        position = end.end()
    parts.append(value[position:])
    return "".join(parts)


def _redact_bot_token(match: re.Match[str]) -> str:
    return f"bot{REDACTED}" if match.group(0).lower().startswith("bot") else REDACTED


def _redact_bearer(match: re.Match[str]) -> str:
    return f"{match.group(1)} {REDACTED}"


def _redact_command_text_match(match: re.Match[str]) -> str:
    return f"{match.group(1)} {REDACTED}"


# 运行时已知密钥：来自 core.config 中名称形如密钥的字符串配置，加上显式注册的值。
_SECRET_CONFIG_SUFFIXES = (
    "_API_KEY",
    "_API_TOKEN",
    "_BOT_TOKEN",
    "_ACCESS_KEY",
    "_PRIVATE_KEY",
    "_SECRET",
    "_PASSWORD",
    "_PASSWD",
)
_registered_secrets: set[str] = set()
# (候选密钥集合, 编译后的匹配式)：候选不变时复用，避免每条日志重新编译。
_known_secret_cache: tuple[frozenset[str], re.Pattern[str] | None] = (frozenset(), None)


def register_secret(value: object) -> None:
    """登记一个需要在所有输出里精确替换的运行时密钥。"""
    if isinstance(value, str) and len(value.strip()) >= MIN_KNOWN_SECRET_LENGTH:
        _registered_secrets.add(value.strip())


def unregister_secret(value: object) -> None:
    if isinstance(value, str):
        _registered_secrets.discard(value.strip())


def _secret_variants(value: str) -> Iterable[str]:
    yield value
    for encoded in (quote(value, safe=""), quote_plus(value)):
        if encoded != value:
            yield encoded


_database_password_cache: tuple[object, str | None] = (None, None)


def _database_password(uri: object) -> str | None:
    """数据库 URI 里的密码，解析结果按 URI 缓存。"""
    global _database_password_cache
    cached_uri, cached_password = _database_password_cache
    if uri == cached_uri:
        return cached_password

    password = None
    if isinstance(uri, str) and "@" in uri:
        try:
            from sqlalchemy.engine import make_url

            password = make_url(uri).password
        except Exception:
            password = None
    _database_password_cache = (uri, password)
    return password


def _config_secret_values() -> set[str]:
    from . import config

    values: set[str] = set()
    for name, value in list(vars(config).items()):
        if name.endswith(_SECRET_CONFIG_SUFFIXES) and isinstance(value, str):
            values.add(value.strip())
    password = _database_password(getattr(config, "SQLALCHEMY_DATABASE_URI", None))
    if password:
        values.add(password)
    return values


def _known_secret_pattern() -> re.Pattern[str] | None:
    global _known_secret_cache
    candidates = frozenset(
        value
        for value in _config_secret_values() | _registered_secrets
        if len(value) >= MIN_KNOWN_SECRET_LENGTH
    )
    cached_candidates, cached_pattern = _known_secret_cache
    if candidates == cached_candidates:
        return cached_pattern

    literals = {
        variant for candidate in candidates for variant in _secret_variants(candidate)
    }
    ordered = sorted(literals, key=lambda item: (-len(item), item))
    pattern = re.compile("|".join(re.escape(item) for item in ordered)) if ordered else None
    _known_secret_cache = (candidates, pattern)
    return pattern


def redact_text(text: object, *, extra_secrets: Iterable[str] = ()) -> str:
    """脱敏自由文本：已知密钥、bot token、鉴权头、URL 凭据与凭据类命令参数。

    ``extra_secrets`` 是调用方已知的敏感原值（如命令参数），不受长度阈值限制。
    函数幂等，对已脱敏文本再次调用不会改变结果。
    """
    if text is None:
        return ""
    value = text if isinstance(text, str) else str(text)
    if not value:
        return value

    for secret in sorted({s for s in extra_secrets if s}, key=len, reverse=True):
        value = value.replace(secret, REDACTED)

    pattern = _known_secret_pattern()
    if pattern is not None:
        value = pattern.sub(REDACTED, value)

    value = _TELEGRAM_BOT_TOKEN.sub(_redact_bot_token, value)
    value = _BEARER_TOKEN.sub(_redact_bearer, value)
    value = _AUTH_HEADER.sub(_redact_auth_header, value)
    value = _COOKIE_HEADER.sub(_redact_cookie_header, value)
    value = _SECRET_KEY_VALUE.sub(_redact_key_value, value)
    value = _URL_USERINFO.sub(_redact_userinfo, value)
    value = _URL_QUERY_PARAM.sub(_redact_query_param, value)
    value = _SENSITIVE_COMMAND_TEXT.sub(_redact_command_text_match, value)
    value = _redact_sql_parameters(value)
    return value


def mask_secret(value: object, *, visible: int = 4) -> str:
    """只保留末尾几位用于核对的掩码，例如 ``****4000``。"""
    text = str(value or "")
    if len(text) <= visible * 2:
        return "****"
    return f"****{text[-visible:]}"


def redact_output(
    text: object,
    *,
    command: str | None = None,
    secrets_to_hide: Iterable[str] = (),
) -> str:
    """脱敏 bot 的可见输出；回复本身就是凭据的命令整体替换为占位文本。"""
    if command in SENSITIVE_OUTPUT_COMMANDS:
        return SENSITIVE_OUTPUT_PLACEHOLDER
    return redact_text(text, extra_secrets=secrets_to_hide)


def message_sanitizer(
    *,
    command: str | None = None,
    secrets_to_hide: Iterable[str] = (),
) -> Callable[[str], str]:
    """返回写入群聊历史前使用的文本清洗函数。"""
    hidden = tuple(secrets_to_hide)

    def sanitize(text: str) -> str:
        if not text:
            return text
        return redact_output(
            redact_command_text(text),
            command=command,
            secrets_to_hide=hidden,
        )

    return sanitize


# --- 异常描述与错误参考 ID --------------------------------------------------


def new_error_ref() -> str:
    """生成给用户和日志共用的错误参考 ID。"""
    return f"ERR-{secrets.token_hex(4).upper()}"


def describe_exception(
    exc: BaseException,
    *,
    limit: int = DEFAULT_SUMMARY_LIMIT,
    extra_secrets: Iterable[str] = (),
) -> str:
    """异常类型加脱敏、截断后的概要，可安全地返回给用户或工具。"""
    name = type(exc).__name__
    try:
        message = " ".join(str(exc).split())
    except Exception:
        message = ""
    message = redact_text(message, extra_secrets=extra_secrets)

    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int) and str(status) not in message:
        message = f"{message} (HTTP {status})".strip()

    if len(message) > limit:
        message = message[: max(limit - 1, 0)] + "…"
    return f"{name}: {message}" if message else name


def format_exception_redacted(
    exc: BaseException,
    *,
    extra_secrets: Iterable[str] = (),
) -> str:
    """脱敏后的完整 traceback 文本，用于诊断日志。"""
    try:
        lines = traceback.format_exception(type(exc), exc, exc.__traceback__)
        return redact_text("".join(lines).rstrip(), extra_secrets=extra_secrets)
    except Exception:
        return describe_exception(exc, extra_secrets=extra_secrets)


def log_exception(
    logger: logging.Logger,
    message: str,
    exc: BaseException | None = None,
    *,
    ref: str | None = None,
    level: int = logging.ERROR,
    include_traceback: bool = True,
    extra_secrets: Iterable[str] = (),
) -> str:
    """记录脱敏后的异常诊断并返回错误参考 ID。

    日志里的细节已在此处脱敏，不依赖 handler 是否安装了脱敏 filter。
    """
    ref = ref or new_error_ref()
    if exc is None:
        exc = sys.exc_info()[1]
    if exc is None:
        detail = ""
    elif include_traceback:
        detail = format_exception_redacted(exc, extra_secrets=extra_secrets)
    else:
        detail = describe_exception(exc, limit=1000, extra_secrets=extra_secrets)
    logger.log(
        level,
        "%s [ref=%s] %s",
        redact_text(message, extra_secrets=extra_secrets),
        ref,
        detail,
    )
    return ref


def user_error_notice(ref: str) -> str:
    """附在用户可见错误回复末尾的参考行。"""
    return f"错误参考 ID / Error ref: {ref}"


def report_error(
    logger: logging.Logger,
    message: str,
    exc: BaseException | None = None,
    **kwargs,
) -> str:
    """记录脱敏诊断，返回可直接追加到用户回复末尾的参考行。"""
    return user_error_notice(log_exception(logger, message, exc, **kwargs))


# --- 日志 filter -------------------------------------------------------------


class RedactingFilter(logging.Filter):
    """对格式化后的日志消息和异常文本脱敏，挂在 handler 上。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "_redacted", False):
            return True
        try:
            message = record.getMessage()
        except Exception:
            # 参数与格式不匹配时交给 handler 按原路径报告，不吞日志。
            return True

        record.msg = redact_text(message)
        record.args = None
        if record.exc_info and not record.exc_text:
            try:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            except Exception:
                record.exc_text = None
        if record.exc_text:
            record.exc_text = redact_text(record.exc_text)
        if record.stack_info:
            record.stack_info = redact_text(record.stack_info)
        record._redacted = True
        return True
