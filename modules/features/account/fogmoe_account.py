from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode, urlparse
from uuid import UUID

import jwt
from aiohttp import BasicAuth, ClientSession, ClientTimeout, web
from jwt import InvalidTokenError, PyJWK
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from core import config, mysql_connection, process_user
from core.command_cooldown import cooldown

logger = logging.getLogger(__name__)

BINDING_REWARD_COINS = 20
TRANSACTION_TTL = timedelta(minutes=10)
METADATA_CACHE_SECONDS = 3600
JWKS_CACHE_SECONDS = 3600
HTTP_TIMEOUT_SECONDS = 10
MAX_JSON_BYTES = 1024 * 1024
RUNNER_BOT_DATA_KEY = "fogmoe_oauth_runner"
UNBIND_CALLBACK_DATA = "fogmoe:unbind"

_metadata_cache: tuple[float, "OAuthMetadata"] | None = None
_jwks_cache: tuple[float, dict[str, Any]] | None = None


class OAuthConfigurationError(RuntimeError):
    """FOGMOE OAuth 配置不完整或不安全。"""


class OAuthFlowError(RuntimeError):
    """OAuth 返回或 token 校验失败。"""


class BindingConflictError(RuntimeError):
    """FOGMOE 与 Telegram 账号已存在冲突绑定。"""


@dataclass(frozen=True)
class OAuthMetadata:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str


@dataclass(frozen=True)
class OAuthTransaction:
    state_hash: str
    telegram_user_id: int
    chat_id: int
    code_verifier: str
    nonce: str
    redirect_uri: str
    requested_scopes: tuple[str, ...]
    action: str
    expected_subject: str | None


@dataclass(frozen=True)
class VerifiedIdentity:
    subject: str
    username: str | None


@dataclass(frozen=True)
class BindResult:
    reward_granted: bool
    username: str | None


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _state_hash(state: str) -> str:
    return hashlib.sha256(state.encode("ascii")).hexdigest()


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _configured_scopes() -> tuple[str, ...]:
    scopes = tuple(
        scope
        for scope in config.FOGMOE_OAUTH_SCOPES.split()
        if scope
    )
    required_scopes = ("openid", "email", "profile")
    if scopes != required_scopes:
        raise OAuthConfigurationError(
            "FOGMOE_OAUTH_SCOPES 必须精确配置为 openid email profile"
        )
    return scopes


def _allowed_algorithms() -> tuple[str, ...]:
    algorithms = tuple(
        value.strip()
        for value in config.FOGMOE_OAUTH_ALLOWED_ALGORITHMS.split(",")
        if value.strip()
    )
    if not algorithms or any(value != "ES256" for value in algorithms):
        raise OAuthConfigurationError(
            "FOGMOE_OAUTH_ALLOWED_ALGORITHMS 当前只允许 ES256"
        )
    return algorithms


def _validate_https_url(value: str, field_name: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or "\\" in value
        or any(char.isspace() for char in value)
    ):
        raise OAuthConfigurationError(f"{field_name} 必须是安全的完整 HTTPS URL")
    return value


def _callback_path() -> str:
    redirect_uri = _validate_https_url(
        config.FOGMOE_OAUTH_REDIRECT_URI,
        "FOGMOE_OAUTH_REDIRECT_URI",
    )
    parsed = urlparse(redirect_uri)
    if parsed.query:
        raise OAuthConfigurationError("FOGMOE_OAUTH_REDIRECT_URI 不能包含 query")
    if not parsed.path or parsed.path == "/":
        raise OAuthConfigurationError("FOGMOE_OAUTH_REDIRECT_URI 必须包含回调路径")
    return parsed.path


def validate_configuration() -> None:
    required = {
        "FOGMOE_OAUTH_DISCOVERY_URL": config.FOGMOE_OAUTH_DISCOVERY_URL,
        "FOGMOE_OAUTH_EXPECTED_ISSUER": config.FOGMOE_OAUTH_EXPECTED_ISSUER,
        "FOGMOE_OAUTH_CLIENT_ID": config.FOGMOE_OAUTH_CLIENT_ID,
        "FOGMOE_OAUTH_CLIENT_SECRET": config.FOGMOE_OAUTH_CLIENT_SECRET,
        "FOGMOE_OAUTH_AUDIENCE": config.FOGMOE_OAUTH_AUDIENCE,
        "FOGMOE_OAUTH_REDIRECT_URI": config.FOGMOE_OAUTH_REDIRECT_URI,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise OAuthConfigurationError(
            "FOGMOE OAuth 已启用，但缺少配置：" + ", ".join(missing)
        )

    _validate_https_url(
        config.FOGMOE_OAUTH_DISCOVERY_URL,
        "FOGMOE_OAUTH_DISCOVERY_URL",
    )
    _validate_https_url(
        config.FOGMOE_OAUTH_EXPECTED_ISSUER,
        "FOGMOE_OAUTH_EXPECTED_ISSUER",
    )
    _callback_path()
    _configured_scopes()
    _allowed_algorithms()


async def _fetch_json(url: str, *, method: str = "GET", **kwargs) -> dict[str, Any]:
    timeout = ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
    async with ClientSession(timeout=timeout) as session:
        async with session.request(method, url, **kwargs) as response:
            body = await response.content.read(MAX_JSON_BYTES + 1)
            if len(body) > MAX_JSON_BYTES:
                raise OAuthFlowError("FOGMOE OAuth 响应过大")
            if not 200 <= response.status < 300:
                raise OAuthFlowError(
                    f"FOGMOE OAuth endpoint 返回 HTTP {response.status}"
                )
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OAuthFlowError("FOGMOE OAuth endpoint 未返回有效 JSON") from exc
    if not isinstance(value, dict):
        raise OAuthFlowError("FOGMOE OAuth endpoint 返回结构无效")
    return value


def _metadata_from_document(document: dict[str, Any]) -> OAuthMetadata:
    expected_issuer = config.FOGMOE_OAUTH_EXPECTED_ISSUER
    issuer = document.get("issuer")
    if issuer != expected_issuer:
        raise OAuthConfigurationError("FOGMOE discovery issuer 与部署配置不一致")

    fields: dict[str, str] = {}
    for name in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        value = document.get(name)
        if not isinstance(value, str):
            raise OAuthConfigurationError(f"FOGMOE discovery 缺少 {name}")
        fields[name] = _validate_https_url(value, f"discovery.{name}")

    return OAuthMetadata(
        issuer=issuer,
        authorization_endpoint=fields["authorization_endpoint"],
        token_endpoint=fields["token_endpoint"],
        jwks_uri=fields["jwks_uri"],
    )


async def get_metadata(*, force_refresh: bool = False) -> OAuthMetadata:
    global _metadata_cache
    now = time.monotonic()
    if (
        not force_refresh
        and _metadata_cache is not None
        and _metadata_cache[0] > now
    ):
        return _metadata_cache[1]

    document = await _fetch_json(config.FOGMOE_OAUTH_DISCOVERY_URL)
    metadata = _metadata_from_document(document)
    _metadata_cache = (now + METADATA_CACHE_SECONDS, metadata)
    return metadata


async def _get_jwks(
    metadata: OAuthMetadata,
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    global _jwks_cache
    now = time.monotonic()
    if not force_refresh and _jwks_cache is not None and _jwks_cache[0] > now:
        return _jwks_cache[1]

    document = await _fetch_json(metadata.jwks_uri)
    keys = document.get("keys")
    if not isinstance(keys, list) or not keys:
        raise OAuthFlowError("FOGMOE JWKS 没有可用签名密钥")
    _jwks_cache = (now + JWKS_CACHE_SECONDS, document)
    return document


def _select_signing_key(
    token: str,
    jwks: dict[str, Any],
    algorithms: tuple[str, ...],
):
    try:
        header = jwt.get_unverified_header(token)
    except InvalidTokenError as exc:
        raise OAuthFlowError("FOGMOE token header 无效") from exc

    algorithm = header.get("alg")
    kid = header.get("kid")
    if algorithm not in algorithms or not isinstance(kid, str) or not kid:
        raise OAuthFlowError("FOGMOE token 使用了不允许的签名参数")

    keys = jwks.get("keys")
    if not isinstance(keys, list):
        raise OAuthFlowError("FOGMOE JWKS 结构无效")
    for value in keys:
        if not isinstance(value, dict) or value.get("kid") != kid:
            continue
        if value.get("use") not in (None, "sig"):
            raise OAuthFlowError("FOGMOE JWK 不是签名密钥")
        if value.get("alg") not in (None, algorithm):
            raise OAuthFlowError("FOGMOE JWK 与 token algorithm 不一致")
        try:
            return PyJWK.from_dict(value, algorithm=algorithm).key
        except (InvalidTokenError, ValueError, TypeError) as exc:
            raise OAuthFlowError("FOGMOE JWK 无法解析") from exc
    return None


def _decode_token(
    token: str,
    key,
    *,
    audience: str,
    algorithms: tuple[str, ...],
) -> dict[str, Any]:
    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=list(algorithms),
            audience=audience,
            issuer=config.FOGMOE_OAUTH_EXPECTED_ISSUER,
            leeway=30,
            options={
                "require": ["iss", "sub", "aud", "exp", "iat"],
                "verify_signature": True,
                "verify_exp": True,
                "verify_iat": True,
                "verify_iss": True,
                "verify_aud": True,
            },
        )
    except InvalidTokenError as exc:
        raise OAuthFlowError("FOGMOE token 验证失败") from exc
    if not isinstance(claims, dict):
        raise OAuthFlowError("FOGMOE token claims 结构无效")
    return claims


async def _decode_with_jwks(
    token: str,
    metadata: OAuthMetadata,
    *,
    audience: str,
) -> dict[str, Any]:
    algorithms = _allowed_algorithms()
    jwks = await _get_jwks(metadata)
    key = _select_signing_key(token, jwks, algorithms)
    if key is None:
        jwks = await _get_jwks(metadata, force_refresh=True)
        key = _select_signing_key(token, jwks, algorithms)
    if key is None:
        raise OAuthFlowError("FOGMOE token kid 不在受信 JWKS 中")
    return _decode_token(
        token,
        key,
        audience=audience,
        algorithms=algorithms,
    )


def _claim_scopes(value: Any) -> set[str]:
    if not isinstance(value, str):
        raise OAuthFlowError("FOGMOE token scope 无效")
    scopes = {item for item in value.split() if item}
    if not scopes:
        raise OAuthFlowError("FOGMOE token scope 为空")
    return scopes


async def verify_token_pair(
    token_response: dict[str, Any],
    transaction: OAuthTransaction,
    metadata: OAuthMetadata,
) -> VerifiedIdentity:
    id_token = token_response.get("id_token")
    access_token = token_response.get("access_token")
    if not isinstance(id_token, str) or not isinstance(access_token, str):
        raise OAuthFlowError("FOGMOE token response 缺少 token")

    id_claims = await _decode_with_jwks(
        id_token,
        metadata,
        audience=config.FOGMOE_OAUTH_CLIENT_ID,
    )
    access_claims = await _decode_with_jwks(
        access_token,
        metadata,
        audience=config.FOGMOE_OAUTH_AUDIENCE,
    )

    nonce = id_claims.get("nonce")
    if not isinstance(nonce, str) or not hmac.compare_digest(
        nonce,
        transaction.nonce,
    ):
        raise OAuthFlowError("FOGMOE ID token nonce 不匹配")

    id_subject = id_claims.get("sub")
    access_subject = access_claims.get("sub")
    if (
        not isinstance(id_subject, str)
        or not isinstance(access_subject, str)
        or not hmac.compare_digest(id_subject, access_subject)
    ):
        raise OAuthFlowError("FOGMOE token subject 不一致")
    try:
        canonical_subject = str(UUID(id_subject))
    except (ValueError, TypeError) as exc:
        raise OAuthFlowError("FOGMOE token subject 不是规范 UUID") from exc
    if canonical_subject != id_subject.lower():
        raise OAuthFlowError("FOGMOE token subject 不是规范 UUID")

    client_id = access_claims.get("client_id")
    if not isinstance(client_id, str) or not hmac.compare_digest(
        client_id,
        config.FOGMOE_OAUTH_CLIENT_ID,
    ):
        raise OAuthFlowError("FOGMOE access token client_id 不匹配")

    token_scopes = _claim_scopes(access_claims.get("scope"))
    response_scopes = _claim_scopes(token_response.get("scope"))
    requested_scopes = set(transaction.requested_scopes)
    if token_scopes != response_scopes or not requested_scopes.issubset(token_scopes):
        raise OAuthFlowError("FOGMOE token scope 与授权请求不一致")

    username = access_claims.get("preferred_username")
    if username is not None:
        if not isinstance(username, str):
            raise OAuthFlowError("FOGMOE preferred_username 类型无效")
        username = username.strip()[:100] or None

    return VerifiedIdentity(subject=canonical_subject, username=username)


async def _store_transaction(
    *,
    state: str,
    telegram_user_id: int,
    chat_id: int,
    code_verifier: str,
    nonce: str,
    redirect_uri: str,
    requested_scopes: tuple[str, ...],
    action: str,
    expected_subject: str | None = None,
) -> None:
    if action not in {"bind", "unbind"}:
        raise ValueError("不支持的 FOGMOE OAuth transaction action")
    if action == "unbind" and expected_subject is None:
        raise ValueError("解绑 transaction 必须包含当前 FOGMOE subject")
    expires_at = _utcnow_naive() + TRANSACTION_TTL
    async with mysql_connection.transaction() as connection:
        await connection.exec_driver_sql(
            "DELETE FROM fogmoe_oauth_transactions "
            "WHERE telegram_user_id = %s OR expires_at <= UTC_TIMESTAMP(6)",
            (telegram_user_id,),
        )
        await connection.exec_driver_sql(
            """
            INSERT INTO fogmoe_oauth_transactions
                (state_hash, telegram_user_id, chat_id, code_verifier, nonce,
                 redirect_uri, requested_scopes, action, expected_subject,
                 expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                _state_hash(state),
                telegram_user_id,
                chat_id,
                code_verifier,
                nonce,
                redirect_uri,
                " ".join(requested_scopes),
                action,
                expected_subject,
                expires_at,
            ),
        )


async def _consume_transaction(state: str) -> OAuthTransaction | None:
    state_hash = _state_hash(state)
    async with mysql_connection.transaction() as connection:
        row = await mysql_connection.fetch_one(
            """
            SELECT state_hash, telegram_user_id, chat_id, code_verifier, nonce,
                   redirect_uri, requested_scopes, action, expected_subject
            FROM fogmoe_oauth_transactions
            WHERE state_hash = %s
              AND consumed_at IS NULL
              AND expires_at > UTC_TIMESTAMP(6)
            FOR UPDATE
            """,
            (state_hash,),
            connection=connection,
        )
        if not row:
            return None
        await connection.exec_driver_sql(
            "UPDATE fogmoe_oauth_transactions "
            "SET consumed_at = UTC_TIMESTAMP(6) WHERE state_hash = %s",
            (state_hash,),
        )
    return OAuthTransaction(
        state_hash=row[0],
        telegram_user_id=int(row[1]),
        chat_id=int(row[2]),
        code_verifier=row[3],
        nonce=row[4],
        redirect_uri=row[5],
        requested_scopes=tuple(str(row[6]).split()),
        action=row[7],
        expected_subject=row[8],
    )


async def _delete_transaction(state_hash: str) -> None:
    await mysql_connection.execute(
        "DELETE FROM fogmoe_oauth_transactions WHERE state_hash = %s",
        (state_hash,),
    )


async def get_binding(telegram_user_id: int) -> dict[str, Any] | None:
    row = await mysql_connection.fetch_one(
        """
        SELECT binding.fogmoe_subject,
               binding.fogmoe_username,
               reward.claimed_at,
               binding.last_verified_at
        FROM fogmoe_account_bindings AS binding
        LEFT JOIN fogmoe_binding_reward_claims AS reward
          ON reward.telegram_user_id = binding.telegram_user_id
        WHERE binding.telegram_user_id = %s
          AND binding.unbound_at IS NULL
        """,
        (telegram_user_id,),
    )
    if not row:
        return None
    return {
        "subject": row[0],
        "username": row[1],
        "reward_granted_at": row[2],
        "last_verified_at": row[3],
    }


async def bind_account(
    telegram_user_id: int,
    identity: VerifiedIdentity,
) -> BindResult:
    try:
        async with mysql_connection.transaction() as connection:
            user_row = await mysql_connection.fetch_one(
                "SELECT id FROM user WHERE id = %s FOR UPDATE",
                (telegram_user_id,),
                connection=connection,
            )
            if not user_row:
                raise BindingConflictError("请先在机器人中使用 /me 完成注册。")

            telegram_binding = await mysql_connection.fetch_one(
                """
                SELECT id, fogmoe_subject
                FROM fogmoe_account_bindings
                WHERE telegram_user_id = %s
                  AND unbound_at IS NULL
                FOR UPDATE
                """,
                (telegram_user_id,),
                connection=connection,
            )
            if telegram_binding:
                if telegram_binding[1] != identity.subject:
                    raise BindingConflictError(
                        "这个 Telegram 账号已经绑定了另一个 FOGMOE Account。"
                    )
                await connection.exec_driver_sql(
                    """
                    UPDATE fogmoe_account_bindings
                    SET fogmoe_username = %s, last_verified_at = UTC_TIMESTAMP(6)
                    WHERE id = %s
                    """,
                    (identity.username, telegram_binding[0]),
                )
                return BindResult(
                    reward_granted=False,
                    username=identity.username,
                )

            subject_binding = await mysql_connection.fetch_one(
                """
                SELECT telegram_user_id
                FROM fogmoe_account_bindings
                WHERE fogmoe_subject = %s
                  AND unbound_at IS NULL
                FOR UPDATE
                """,
                (identity.subject,),
                connection=connection,
            )
            if subject_binding:
                raise BindingConflictError(
                    "这个 FOGMOE Account 已经绑定了其他 Telegram 账号。"
                )

            await connection.exec_driver_sql(
                """
                INSERT INTO fogmoe_account_bindings
                    (telegram_user_id, fogmoe_subject, fogmoe_username,
                     last_verified_at)
                VALUES (%s, %s, %s, UTC_TIMESTAMP(6))
                """,
                (telegram_user_id, identity.subject, identity.username),
            )
            reward_claim = await mysql_connection.fetch_one(
                """
                SELECT telegram_user_id
                FROM fogmoe_binding_reward_claims
                WHERE telegram_user_id = %s
                   OR fogmoe_subject = %s
                LIMIT 1
                FOR UPDATE
                """,
                (telegram_user_id, identity.subject),
                connection=connection,
            )
            reward_granted = reward_claim is None
            if reward_granted:
                await connection.exec_driver_sql(
                    """
                    INSERT INTO fogmoe_binding_reward_claims
                        (telegram_user_id, fogmoe_subject, claimed_at)
                    VALUES (%s, %s, UTC_TIMESTAMP(6))
                    """,
                    (telegram_user_id, identity.subject),
                )
                rewarded = await process_user.add_free_coins(
                    telegram_user_id,
                    BINDING_REWARD_COINS,
                    connection=connection,
                )
                if rewarded != BINDING_REWARD_COINS:
                    raise RuntimeError("FOGMOE 绑定奖励写入失败")
    except IntegrityError as exc:
        raise BindingConflictError(
            "账号绑定状态刚刚发生变化，请返回 Telegram 后重试 /fogmoe。"
        ) from exc

    return BindResult(
        reward_granted=reward_granted,
        username=identity.username,
    )


async def unbind_account(
    telegram_user_id: int,
    identity: VerifiedIdentity,
    expected_subject: str,
) -> str | None:
    if not hmac.compare_digest(identity.subject, expected_subject):
        raise BindingConflictError(
            "登录的不是当前绑定的 FOGMOE Account，账号未解绑。"
        )

    async with mysql_connection.transaction() as connection:
        binding = await mysql_connection.fetch_one(
            """
            SELECT id, fogmoe_subject, fogmoe_username
            FROM fogmoe_account_bindings
            WHERE telegram_user_id = %s
              AND unbound_at IS NULL
            FOR UPDATE
            """,
            (telegram_user_id,),
            connection=connection,
        )
        if not binding:
            raise BindingConflictError(
                "当前没有已绑定的 FOGMOE Account，无需解绑。"
            )
        if not hmac.compare_digest(binding[1], identity.subject):
            raise BindingConflictError(
                "绑定状态已发生变化，请返回 Telegram 重新发送 /fogmoe，"
                "再点击“解绑或更换账号”。"
            )
        await connection.exec_driver_sql(
            """
            UPDATE fogmoe_account_bindings
            SET unbound_at = UTC_TIMESTAMP(6),
                last_verified_at = UTC_TIMESTAMP(6)
            WHERE id = %s
              AND unbound_at IS NULL
            """,
            (binding[0],),
        )
        return binding[2]


async def _exchange_code(
    code: str,
    transaction: OAuthTransaction,
    metadata: OAuthMetadata,
) -> dict[str, Any]:
    return await _fetch_json(
        metadata.token_endpoint,
        method="POST",
        auth=BasicAuth(
            config.FOGMOE_OAUTH_CLIENT_ID,
            config.FOGMOE_OAUTH_CLIENT_SECRET,
        ),
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": transaction.redirect_uri,
            "code_verifier": transaction.code_verifier,
        },
        headers={"Accept": "application/json"},
    )


async def _create_authorization_url(
    *,
    telegram_user_id: int,
    chat_id: int,
    action: str,
    expected_subject: str | None = None,
) -> str:
    metadata = await get_metadata()
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    scopes = _configured_scopes()
    await _store_transaction(
        state=state,
        telegram_user_id=telegram_user_id,
        chat_id=chat_id,
        code_verifier=verifier,
        nonce=nonce,
        redirect_uri=config.FOGMOE_OAUTH_REDIRECT_URI,
        requested_scopes=scopes,
        action=action,
        expected_subject=expected_subject,
    )
    return (
        metadata.authorization_endpoint
        + "?"
        + urlencode(
            {
                "response_type": "code",
                "client_id": config.FOGMOE_OAUTH_CLIENT_ID,
                "redirect_uri": config.FOGMOE_OAUTH_REDIRECT_URI,
                "scope": " ".join(scopes),
                "state": state,
                "nonce": nonce,
                "code_challenge": _pkce_challenge(verifier),
                "code_challenge_method": "S256",
            }
        )
    )


def _query_value(request: web.Request, name: str, *, max_length: int) -> str | None:
    values = request.query.getall(name, [])
    if len(values) > 1:
        raise OAuthFlowError(f"OAuth callback 包含重复的 {name}")
    if not values:
        return None
    value = values[0]
    if not value or len(value) > max_length:
        raise OAuthFlowError(f"OAuth callback 的 {name} 无效")
    return value


def _html_response(title: str, message: str, *, status: int = 200) -> web.Response:
    body = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
</head>
<body>
  <main>
    <h1>{html.escape(title)}</h1>
    <p>{html.escape(message)}</p>
    <p>你现在可以关闭此页面并返回 Telegram。</p>
  </main>
</body>
</html>
"""
    return web.Response(
        text=body,
        status=status,
        content_type="text/html",
        charset="utf-8",
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
        },
    )


async def _notify(application, chat_id: int, message: str) -> None:
    try:
        await application.bot.send_message(chat_id=chat_id, text=message)
    except Exception:
        logger.exception("FOGMOE Account 操作结果已落库，但 Telegram 通知发送失败")


async def oauth_callback(
    request: web.Request,
    telegram_application,
) -> web.Response:
    transaction: OAuthTransaction | None = None
    try:
        state = _query_value(request, "state", max_length=512)
        if state is None:
            raise OAuthFlowError("OAuth callback 缺少 state")
        try:
            state.encode("ascii")
        except UnicodeEncodeError as exc:
            raise OAuthFlowError("OAuth callback 的 state 无效") from exc

        transaction = await _consume_transaction(state)
        if transaction is None:
            return _html_response(
                "认证链接无效",
                "该链接已使用或已过期，请返回 Telegram 重新发起操作。",
                status=400,
            )

        error = _query_value(request, "error", max_length=200)
        if error is not None:
            if transaction.action == "unbind":
                notification = (
                    "FOGMOE Account 解绑已取消，当前绑定保持不变。"
                )
                title = "已取消解绑"
                message = "当前 FOGMOE Account 仍然绑定到 Telegram。"
            else:
                notification = (
                    "FOGMOE Account 绑定已取消。需要时可重新发送 /fogmoe。"
                )
                title = "已取消绑定"
                message = "FOGMOE Account 没有绑定到 Telegram。"
            await _notify(
                telegram_application,
                transaction.chat_id,
                notification,
            )
            return _html_response(title, message)

        code = _query_value(request, "code", max_length=4096)
        if code is None:
            raise OAuthFlowError("OAuth callback 缺少 authorization code")

        metadata = await get_metadata()
        token_response = await _exchange_code(code, transaction, metadata)
        identity = await verify_token_pair(token_response, transaction, metadata)
        if transaction.action == "unbind":
            if transaction.expected_subject is None:
                raise OAuthFlowError("解绑 transaction 缺少预期 subject")
            await unbind_account(
                transaction.telegram_user_id,
                identity,
                transaction.expected_subject,
            )
            notification = (
                "FOGMOE Account 已解绑。现在可以发送 /fogmoe 绑定其他账号；"
                "已经领取的 20 金币不会扣除，也不会因换绑再次发放。"
            )
            title = "解绑成功"
        else:
            result = await bind_account(transaction.telegram_user_id, identity)
            if result.reward_granted:
                notification = (
                    "FOGMOE Account 绑定成功！"
                    f"首次绑定奖励 {BINDING_REWARD_COINS} 金币已到账。"
                )
            else:
                notification = (
                    "FOGMOE Account 绑定成功。首次绑定奖励已经领取过，"
                    "本次不会重复发放。"
                )
            title = "绑定成功"
        await _notify(telegram_application, transaction.chat_id, notification)
        return _html_response(title, notification)
    except BindingConflictError as exc:
        if transaction is not None:
            await _notify(telegram_application, transaction.chat_id, str(exc))
        action_name = (
            "解绑"
            if transaction is not None and transaction.action == "unbind"
            else "绑定"
        )
        return _html_response(f"无法完成{action_name}", str(exc), status=409)
    except (OAuthConfigurationError, OAuthFlowError, SQLAlchemyError) as exc:
        logger.warning("FOGMOE OAuth callback 失败：%s", type(exc).__name__)
        is_unbind = transaction is not None and transaction.action == "unbind"
        if transaction is not None:
            if is_unbind:
                failure_message = (
                    "FOGMOE Account 解绑失败，当前绑定保持不变。"
                    "请返回 Telegram 后重新发送 /fogmoe，"
                    "再点击“解绑或更换账号”。"
                )
            else:
                failure_message = (
                    "FOGMOE Account 绑定失败，请返回 Telegram 后重新发送 /fogmoe。"
                )
            await _notify(
                telegram_application,
                transaction.chat_id,
                failure_message,
            )
        if is_unbind:
            title = "解绑失败"
            browser_message = (
                "本次认证未完成，当前绑定保持不变。"
                "请返回 Telegram 后重新发送 /fogmoe，"
                "再点击“解绑或更换账号”。"
            )
        else:
            title = "绑定失败"
            browser_message = (
                "本次认证未完成，请返回 Telegram 后重新发送 /fogmoe。"
            )
        return _html_response(
            title,
            browser_message,
            status=400,
        )
    except Exception:
        logger.exception("FOGMOE OAuth callback 出现未预期错误")
        is_unbind = transaction is not None and transaction.action == "unbind"
        if transaction is not None:
            if is_unbind:
                notification = (
                    "FOGMOE Account 解绑暂时失败，当前绑定保持不变。"
                    "请稍后重新发送 /fogmoe，再点击“解绑或更换账号”。"
                )
            else:
                notification = (
                    "FOGMOE Account 绑定暂时失败，请稍后重新发送 /fogmoe。"
                )
            await _notify(
                telegram_application,
                transaction.chat_id,
                notification,
            )
        return _html_response(
            "解绑暂时不可用" if is_unbind else "绑定暂时不可用",
            "请稍后返回 Telegram 重新尝试。",
            status=500,
        )
    finally:
        if transaction is not None:
            try:
                await _delete_transaction(transaction.state_hash)
            except SQLAlchemyError:
                logger.exception("清理已终止的 FOGMOE OAuth transaction 失败")


@cooldown
async def fogmoe_account_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.message.reply_text(
            "为保护账号隐私，请私聊机器人后发送 /fogmoe。"
        )
        return
    if not config.FOGMOE_OAUTH_ENABLED:
        await update.message.reply_text("FOGMOE Account 绑定功能当前未启用。")
        return

    telegram_user_id = update.effective_user.id
    if not await process_user.async_user_exists(telegram_user_id):
        await update.message.reply_text(
            "请先发送 /me 完成机器人注册，再绑定 FOGMOE Account。"
        )
        return

    try:
        validate_configuration()
        binding = await get_binding(telegram_user_id)
        if binding is not None:
            display_name = binding["username"] or "已验证账号"
            keyboard = InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton(
                        "解绑或更换账号",
                        callback_data=UNBIND_CALLBACK_DATA,
                    )
                ]]
            )
            await update.message.reply_text(
                f"已绑定 FOGMOE Account：{display_name}\n"
                "首次绑定的 20 金币奖励只能领取一次。",
                reply_markup=keyboard,
            )
            return

        authorization_url = await _create_authorization_url(
            telegram_user_id=telegram_user_id,
            chat_id=update.effective_chat.id,
            action="bind",
        )
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("使用 FOGMOE Account 绑定", url=authorization_url)]]
        )
        await update.message.reply_text(
            "点击下方按钮登录 FOGMOE Account，并授权邮箱和用户名信息。\n"
            "首次成功绑定可获得 20 金币；每个 Telegram 与 FOGMOE Account "
            "只能一对一绑定。链接 10 分钟内有效。",
            reply_markup=keyboard,
        )
    except (OAuthConfigurationError, OAuthFlowError, SQLAlchemyError):
        logger.exception("创建 FOGMOE OAuth authorization request 失败")
        await update.message.reply_text(
            "暂时无法开始 FOGMOE Account 绑定，请稍后重试。"
        )


async def fogmoe_account_unbind_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()

    if update.effective_chat.type != ChatType.PRIVATE:
        await query.edit_message_text(
            "为保护账号安全，请私聊机器人后发送 /fogmoe。"
        )
        return
    if not config.FOGMOE_OAUTH_ENABLED:
        await query.edit_message_text("FOGMOE Account 绑定功能当前未启用。")
        return

    telegram_user_id = update.effective_user.id
    if not await process_user.async_user_exists(telegram_user_id):
        await query.edit_message_text(
            "请先发送 /me 完成机器人注册。"
        )
        return

    try:
        validate_configuration()
        binding = await get_binding(telegram_user_id)
        if binding is None:
            await query.edit_message_text(
                "当前没有已绑定的 FOGMOE Account。\n"
                "如需绑定，请发送 /fogmoe。"
            )
            return

        authorization_url = await _create_authorization_url(
            telegram_user_id=telegram_user_id,
            chat_id=update.effective_chat.id,
            action="unbind",
            expected_subject=binding["subject"],
        )
        keyboard = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton(
                    "验证并解绑 FOGMOE Account",
                    url=authorization_url,
                )
            ]]
        )
        display_name = binding["username"] or "当前账号"
        await query.edit_message_text(
            f"即将解绑 FOGMOE Account：{display_name}\n"
            "请使用当前已绑定的 FOGMOE Account 再次登录确认。\n"
            "解绑后可以绑定其他账号；已经领取的 20 金币不会扣除，"
            "也不会因换绑再次发放。链接 10 分钟内有效。",
            reply_markup=keyboard,
        )
    except (OAuthConfigurationError, OAuthFlowError, SQLAlchemyError):
        logger.exception("创建 FOGMOE OAuth unbind request 失败")
        await query.edit_message_text(
            "暂时无法开始 FOGMOE Account 解绑，请稍后重试。"
        )


def setup_fogmoe_account_handlers(application) -> None:
    application.add_handler(CommandHandler("fogmoe", fogmoe_account_command))
    application.add_handler(
        CallbackQueryHandler(
            fogmoe_account_unbind_callback,
            pattern=r"^fogmoe:unbind$",
        )
    )


async def start_callback_server(telegram_application) -> None:
    if not config.FOGMOE_OAUTH_ENABLED:
        return
    validate_configuration()

    callback_path = _callback_path()
    app = web.Application(client_max_size=1024)

    async def callback_handler(request: web.Request) -> web.Response:
        return await oauth_callback(request, telegram_application)

    app.add_routes([web.get(callback_path, callback_handler)])
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        site = web.TCPSite(
            runner,
            config.FOGMOE_OAUTH_LISTEN_HOST,
            config.FOGMOE_OAUTH_LISTEN_PORT,
        )
        await site.start()
    except Exception:
        await runner.cleanup()
        raise
    telegram_application.bot_data[RUNNER_BOT_DATA_KEY] = runner
    logger.info(
        "FOGMOE OAuth callback server listening on %s:%s",
        config.FOGMOE_OAUTH_LISTEN_HOST,
        config.FOGMOE_OAUTH_LISTEN_PORT,
    )


async def stop_callback_server(telegram_application) -> None:
    runner = telegram_application.bot_data.pop(RUNNER_BOT_DATA_KEY, None)
    if runner is not None:
        await runner.cleanup()
